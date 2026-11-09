# ADR-022: The MCP server is its own authorization server, with GitHub as the login

Date: 2026-11-09
Status: Accepted

## Context

The requirement changed: the tools must be usable from **any** AI chat the team
can reach, not only from Copilot Chat in VS Code. ADR-021 settled the first
half of that — depend on nothing beyond tools, because tools are the only
capability every client implements. This ADR settles the second half: how a
client that cannot launch a local process connects at all.

Desktop clients (Copilot, Claude Code, Claude Desktop, Cursor) launch the
server over stdio and work today. Browser-based chats — claude.ai, ChatGPT —
cannot. They need a URL, and a URL needs authentication that is not a shared
secret, because:

- **The shared service token is not an identity.** On the http transport today,
  a caller presents `INTERNAL_API_TOKEN` and then an `X-Actor-Email` header
  naming whoever it likes. The token proves the caller is a trusted peer; it
  does nothing to prove *who* the request is for. ADR-014 recorded this and
  nothing has fixed it. Exposing that port beyond loopback would mean anyone
  holding one token can read any project any registered user can.
- **A shared token cannot be given to a browser client.** It would have to be
  pasted into claude.ai's settings by every user, and it is the same secret for
  all of them.

The MCP specification's answer is OAuth 2.1. The question is which side we sit
on, and the answer is forced by one detail: **claude.ai registers itself with
the server dynamically** (RFC 7591 dynamic client registration). GitHub's OAuth
does not support DCR, so GitHub cannot be the authorization server a chat
client talks to.

## Decision

**This MCP server is an OAuth 2.1 authorization server. The human login is
delegated upstream to GitHub. The access token carries the user's email, and
that email is the identity for everything downstream.**

1. **The SDK provides the endpoints.** `MCPServer(auth_server_provider=…,
   auth=AuthSettings(…))` mounts `/authorize`, `/token`, `/register`,
   `/revoke` and the metadata documents, with
   `ClientRegistrationOptions(enabled=True)` so claude.ai can register itself.
   PKCE is verified by the SDK's token handler; the provider only has to store
   the `code_challenge` it was given.

2. **GitHub performs the login, and we never see a password.** `authorize()`
   returns a redirect to GitHub. A `custom_route` at
   `/auth/github/callback` exchanges the code for a GitHub token, reads the
   account's **primary, verified** email from `GET /user/emails`, and then
   issues *our* authorization code with that email as its `subject`. A GitHub
   account with no verified email is refused: an unverified address is a claim,
   not a fact.

3. **The GitHub token is used once and discarded.** It is exchanged for an
   email and never stored. This platform does not act on GitHub's behalf and
   needs no GitHub scope beyond `user:email`.

4. **Our access tokens are stateless and signed.** HMAC-SHA256 over a payload
   of email, client id, scopes, resource and expiry, keyed by a secret from the
   environment. `load_access_token` verifies the signature and expiry, with no
   lookup. The consequence is accepted deliberately and recorded below: a token
   cannot be revoked before it expires, so expiry is short.

5. **Authorization codes and registered clients are in Redis**, which the stack
   already runs. Codes are single-use with a 60-second lifetime, which needs a
   store — a signed code could be replayed, and PKCE alone does not stop the
   holder of the verifier replaying it. Registered clients must survive a
   restart or every client has to re-register after a deploy.

6. **`X-Actor-Email` is retired on the http transport.** The actor comes from
   `get_access_token().subject`. A request carrying the header is not refused;
   the header is **ignored**, and ignored loudly in the log once per process,
   because silently honouring it would reintroduce exactly the hole this ADR
   closes.

7. **One transitional mode, named and loud.** `MCP_AUTH_MODE` takes `oauth`
   (the default) or `service_token` (the pre-ADR behaviour: shared token plus
   `X-Actor-Email`). The local `mcp` container in `docker-compose.yml` sets
   `service_token` explicitly, with a comment saying why, so `docker compose
   up` keeps working before anyone has created a GitHub OAuth app. The mode is
   logged at startup, and `service_token` logs a warning naming what it trusts.
   It exists so the cutover is a variable rather than a flag day; it is not a
   supported way to expose the port.

8. **stdio is unchanged.** `QA_ACTOR_EMAIL` and the process boundary remain the
   identity there (ADR-014). A developer running the server themselves is not
   made safer by an OAuth round trip, and the desktop clients that work today
   keep working.

## Alternatives

- **Be a resource server and let an external IdP be the authorization server.**
  Cleaner in principle: no tokens to mint, no authorization code to store. This
  is the right answer with Microsoft Entra ID, which supports DCR for this
  purpose. Rejected for now because the chosen IdP is GitHub, which does not —
  so a client that registers dynamically cannot be served. The `TokenVerifier`
  path stays available in the SDK, and switching to Entra later replaces one
  module.
- **Microsoft Entra ID as the IdP.** The better long-term answer if company
  identity lives there: real SSO, conditional access, group claims, and
  deprovisioning when someone leaves. Rejected for this step only because it
  needs a tenant admin to register an application, and the point of this step
  is to get remote access working. The upstream is one module.
- **Keep the shared token and add per-user tokens the operator issues by hand.**
  No OAuth, no round trip, and the CLI already manages users. Rejected: it
  means a long-lived bearer secret per person, pasted into a chat client's
  settings, with no expiry and no revocation path. That is a worse version of
  the problem, with more admin.
- **Stateful access tokens in Redis, so they can be revoked.** Rejected for
  this step: it puts a Redis round trip on every tool call and makes the
  gatekeeper's availability depend on it. Revisit when there is a reason to
  revoke a token before it expires — a departing employee is handled by
  deactivating the user, which the access check honours on the next call
  anyway.
- **Store registered clients and codes in Postgres.** Rejected: ADR-003 keeps
  the gatekeeper away from the database, and that is the boundary that makes the
  split worth having. Redis is session state, not data.

## Consequences

- **The gatekeeper gains a Redis dependency.** It previously held no state at
  all. This is a real widening of ADR-003's boundary, justified by session
  state being neither business data nor business logic: a lost Redis means
  people re-authenticate, not that anything is wrong. `docker-compose.yml`
  grows one variable on the `mcp` service, and it stays an explicit list rather
  than an `env_file`.
- **Two new secrets**: the GitHub OAuth app's client secret, and the signing
  key for our tokens. Both from the environment, both covered by the startup
  placeholder warning, and the signing key must be generated per environment —
  a shared one would let one environment mint tokens for another.
- **A token cannot be revoked before it expires.** Access tokens live one hour,
  refresh tokens one day. The mitigation is that authorisation is re-checked on
  every call: a user who is deactivated or loses a project grant is refused on
  the next tool call regardless of their token. The token proves *who*, never
  *what they may do*.
- **The audit trail gets stronger.** Today `actor_email` is a header a trusted
  peer asserted. After this, it is an email GitHub verified and we signed.
  Rows written before and after this change are not equally trustworthy, and
  nothing in the table records which regime produced a row — the `source`
  column distinguishes `mcp` from `api`, not one kind of identity from another.
- **The HTTP transport becomes genuinely exposable**, which is what makes this
  step worth doing, and also means the next mistake has a larger blast radius.
  Public exposure is a separate step with a separate review: a tunnel or a real
  host, TLS, and the GitHub app's callback URL pointing at it.
- **A transitional mode means the hole is closed by configuration, not by
  code.** `MCP_AUTH_MODE=service_token` still trusts the actor header. Nothing
  stops a deployment setting it, which is why it is logged at startup and why
  the variable is named for what it does.
- **Anyone with a GitHub account can complete the login**, and then be refused
  by the platform because they are not a registered user. That is the correct
  order — authenticate, then authorise — but it means the first thing a new
  colleague sees is "this account is not registered". The message says what to
  do about it.

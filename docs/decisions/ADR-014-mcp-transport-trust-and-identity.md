# ADR-014: Two transports, two trust models, and who a tool call is for

Date: 2026-11-08
Status: Accepted

## Context

The MCP server exists to answer one question before it forwards anything:
**who is this call for?** The backend authorises against that answer — a grant
in `user_project_access` for that specific person — so getting it wrong does
not produce an error, it produces one user reading another user's projects.

ADR-004 chose to support both transports. They have nothing in common here.

**stdio.** Copilot Chat starts this process itself, as the developer, and
speaks to it over the process's own pipes. Nothing else can reach it; there is
one caller for the lifetime of the process.

**http.** One long-lived process serves many callers over a socket. The SDK can
expose per-request headers via `Context.headers`, which is `None` on stdio. Its
own docstring is worth quoting: *"Headers are client-supplied input — never
treat one as an identity assertion."*

That warning is the crux. If the HTTP transport read `X-Actor-Email` and
trusted it, anyone who could reach port 8765 could name any colleague and read
their projects. The port is bound to loopback in `docker-compose.yml`, but
loopback on a shared host is not a boundary, and a future deployment will move
it.

Separately, `.env` holds `QA_ACTOR_EMAIL` because stdio needs it. If the HTTP
transport also fell back to that variable, a container would start cleanly and
attribute every call in the audit trail to one person — the worst outcome,
because it looks like it is working.

## Decision

**stdio takes the actor from the environment.** `QA_ACTOR_EMAIL` is required,
and `check_startup_identity` refuses to start without it. The process boundary
*is* the identity: Copilot Chat launched this process as this developer.

As a guard, a stdio-configured server that receives a call *with* headers
refuses it. That should be impossible; if a future transport makes it possible,
the environment identity must not be applied to a request from elsewhere.

**http requires two things per request.** First the shared service token, in
`Authorization: Bearer`, compared with `hmac.compare_digest`. Only then is
`X-Actor-Email` read. The token is what makes the actor header a claim from a
trusted peer instead of from anyone who can reach the socket.

**http ignores `QA_ACTOR_EMAIL` entirely.** Not as a fallback, not as a
default. If it is set, the server logs a warning at startup saying it is
ignored. The `mcp` service in `docker-compose.yml` does not pass it at all.

**The `mcp` container receives no database or Jira credentials.** It has no
`env_file`; its seven environment variables are listed individually. The
gatekeeper cannot reach Postgres or Jira even if it is fully compromised. This
is also why `mcp_server/config.py` defines its own settings class rather than
importing `app.core.config.Settings`, which would require `DATABASE_URL`.

**Failures before identity are not audited.** There is no actor to attribute a
record to, and the backend rejects a row without one. They are logged.

## Alternatives considered

**Trust `X-Actor-Email` on http with no token.** Simplest, and defensible
behind a mesh that already authenticates. Rejected: nothing in this deployment
provides that, the SDK explicitly warns against it, and the failure mode is
silent impersonation rather than an error.

**Use `QA_ACTOR_EMAIL` on http as a fallback when the header is absent.**
Would make the container work out of the box for a demo. Rejected, firmly. It
is the single most dangerous convenience available here: the stack comes up,
calls succeed, and every audit row and every access check names one person.
A refusal is noisy; a wrong attribution is invisible.

**Real per-user authentication on the MCP server — OAuth, which the SDK
supports via `auth_server_provider` and `TokenVerifier`.** The correct
long-term answer, and the SDK is built for it. Rejected for Phase 1 as a large
piece of work the brief does not ask for: it needs an identity provider,
redirect handling, and token storage. The shared token plus actor header is
ADR-003's decision, and this ADR only settles how it applies per transport.
Phase 3 should revisit it before the HTTP transport is exposed beyond loopback.

**Drop the HTTP transport and ship stdio only.** Smallest honest scope, and
Copilot Chat uses stdio anyway. Rejected because `docker compose up` would
still not bring up the whole stack, which the brief names as a completion
criterion, and because a remote MCP client would have nothing to reach.

**Have the backend decide, by passing the raw header straight through.** The
backend already validates the actor. Rejected: the MCP server would then write
audit rows for an unvalidated identity, and a malformed address would reach the
database lookup rather than being refused at the door.

## Consequences

- A stdio server is tied to one person for its lifetime. That is correct for
  Copilot Chat and wrong for anything else; the HTTP transport is the answer
  for anything else.
- The HTTP transport needs the service token distributed to every caller. That
  token authorises *any* actor claim, so it is as sensitive as it sounds:
  whoever holds it can impersonate any registered user. It is the main residual
  risk in Phase 1 and the reason to revisit OAuth before the port is exposed.
- An MCP call now produces two audit rows: `source=mcp` for the tool
  invocation and `source=api` for the data access. They share a `request_id`,
  so they correlate. Anyone counting reads must filter on `source`.
- Validation failures at the MCP layer produce an `mcp` row with no `api` row,
  which is exactly the signal the gatekeeper exists to provide — a run of them
  from one actor is someone probing.
- The gatekeeper holds no database or Jira credentials, verified by inspecting
  the resolved Compose configuration rather than by reading the file.
- `check_startup_identity` means a misconfigured stdio server fails at startup
  rather than on the first tool call, where the error would surface inside
  Copilot Chat as an unexplained failure.

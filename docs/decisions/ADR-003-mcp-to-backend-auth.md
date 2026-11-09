# ADR-003: MCP server authenticates to the backend with a shared service token

Date: 2026-11-08
Status: Accepted

## Context

The MCP server runs on a developer's machine over stdio, launched by GitHub
Copilot Chat. It forwards every tool call to the FastAPI backend. The backend
must know two things about each request: that it came from our MCP server, and
which human it was acting for, because every call is audited.

MCP over stdio has no built-in notion of an authenticated caller. Whatever
identity the backend sees has to be supplied by the MCP server.

## Decision

1. **Service token for the channel.** The MCP server sends
   `Authorization: Bearer <INTERNAL_API_TOKEN>`. The backend compares it against
   its own copy of `INTERNAL_API_TOKEN` using `hmac.compare_digest`, so the
   comparison does not leak the token through timing.
2. **Actor header for the person.** The MCP server sends
   `X-Actor-Email: <QA_ACTOR_EMAIL>`. The backend resolves that email to a row
   in `users`, and uses it as the subject of the access check and the audit
   record.
3. **The actor header is trusted only because the service token is valid.** The
   backend rejects the request outright if the token is missing or wrong; it
   never reads the actor header from an unauthenticated request.
4. **Both values come from the environment.** The MCP server never prompts for
   them and never writes them to a log.

## Alternatives

- **Short-lived signed JWT**, minted per call by the MCP server with the actor as
  `sub`. Rejected for Phase 1: it needs a signing secret distributed to exactly
  the same places as the shared token, so it buys no additional trust while
  adding a dependency and a clock-skew failure mode. It becomes worthwhile when
  the MCP server is hosted centrally and issues tokens for many users.
- **mTLS between the MCP server and the backend.** Strong, and appropriate once
  both run as deployed services. Too much certificate plumbing for a process
  Copilot launches on a laptop.
- **No auth, trusting localhost.** Rejected. Without a verified actor the audit
  log cannot answer "who asked for this issue", which is the main reason the
  audit table exists.
- **Letting the backend read the OS user.** Not available: the backend is a
  separate container and sees only the HTTP request.

## Consequences

- `X-Actor-Email` is self-asserted by whoever holds the service token. Anyone
  with `INTERNAL_API_TOKEN` can claim to be any user. This is acceptable only
  while the token stays on trusted developer machines, and it is the single
  reason this ADR will need revisiting before the platform is multi-tenant or
  internet-facing. Treat `INTERNAL_API_TOKEN` as a high-value secret.
- The token is shared, so rotating it means restarting every MCP client. Phase 1
  accepts that; a central MCP deployment would make rotation a server-side
  concern.
- The backend needs a rule for an unknown actor email. Step 5 and Step 6 decide
  between rejecting with 403 and creating the user row on first sight; the
  choice gets recorded there.
- Because the token is one value for the whole channel, per-tool permissions
  must come from the actor's `user_project_access` rows, not from the token.

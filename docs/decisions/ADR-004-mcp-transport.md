# ADR-004: The MCP server supports both stdio and HTTP transport

Date: 2026-11-08
Status: Accepted

## Context

Step 2 puts the MCP server in `docker-compose.yml`, and the project's
completion criteria include "`docker compose up` starts all services".

MCP servers for editor clients normally speak over **stdio**: the client starts
the server as a child process and talks to it on stdin and stdout. That is how
GitHub Copilot Chat will launch ours, configured from `.vscode/mcp.json` in
Step 8.

A stdio process has no useful life inside Compose. There is no client attached
to its stdin, so it either exits immediately or sits idle with no port to health
check. Writing a container that pretends otherwise produces a service that looks
up but can never be called.

The MCP specification also defines a **streamable HTTP** transport, which the
official Python SDK implements, and FastMCP can serve either.

## Decision

The MCP server chooses its transport from the environment:

- `MCP_TRANSPORT=stdio` (the default, and what `.env.example` ships) — Copilot
  Chat and any other local editor client. `MCP_HOST` and `MCP_PORT` are ignored.
- `MCP_TRANSPORT=http` — listens on `MCP_HOST:MCP_PORT`, default `8765`. This is
  what the `mcp` service in `docker-compose.yml` sets.

Everything else is identical between the two: the same tool registration, the
same validation, the same audit records, the same backend client. Transport is
chosen in one place in `mcp_server/server.py` (Step 7) and nothing else in the
package knows which one is active.

The container health check is a TCP connect to the port rather than an HTTP
request, because the MCP HTTP endpoint expects a session-initialising POST and
would reject a bare probe.

## Alternatives

- **Leave the MCP server out of compose entirely.** Honest, and arguably
  correct for Phase 1, since the only real client is local. Rejected because the
  brief asks for it in compose, and because having no deployable form of the
  gatekeeper hides the question of how it will eventually be hosted.
- **Put it in compose with a `profiles:` key so it never starts by default.**
  This is the tidy way to express "not part of the default stack", but it means
  `docker compose up` does not start all services, which contradicts the
  completion criteria.
- **Run it in compose with `stdin_open: true` and `tty: true`.** The container
  stays alive, but nothing can talk to it, and a health check would be a lie.
  Rejected.
- **HTTP only, with a local stdio-to-HTTP bridge for Copilot.** Fewer code paths
  in our server, but it adds a proxy process on every developer machine and a
  second place for auth to go wrong. Rejected.
- **SSE transport.** Superseded by streamable HTTP in the MCP specification.
  Not worth adopting for new code.

## Consequences

- There are two transports to keep working, so Step 7 must exercise both. The
  shared surface is large and the differing surface is one function call, which
  keeps that cheap.
- The HTTP transport exposes the gatekeeper on a port. In compose it is bound to
  `127.0.0.1` only. Before this is ever deployed beyond a laptop, the HTTP
  listener needs caller authentication of its own: `INTERNAL_API_TOKEN`
  authenticates the MCP server *to the backend*, and authenticates nothing *to
  the MCP server*. ADR-003 already flags this; the HTTP transport makes it
  reachable over a network, which raises the priority. Phase 1 accepts it
  because the bind is loopback-only.
- Copilot Chat will talk to a stdio process started from `.vscode/mcp.json`,
  **not** to the `mcp` container. Two instances can therefore be running at
  once against the same backend. That is harmless — both are stateless and both
  audit through the backend — but it surprises people debugging logs, so the
  compose file says so in a comment.
- `MCP_PORT` becomes a reserved port in local development. 8765 was chosen to
  sit clear of the API (8000), Postgres (5432), Redis (6379) and MinIO
  (9000/9001).

## Addendum, Step 7

Two things in this ADR were written before the server existed and need
qualifying:

- References to **FastMCP** describe the SDK's high-level server API, which is
  called `MCPServer` in `mcp` 2.x. See
  [ADR-015](ADR-015-mcp-sdk-version.md).
- The two transports turn out to have materially different **trust models**,
  not just different plumbing: stdio takes its identity from the environment,
  while http must verify a token before it may believe an actor header. That is
  settled in [ADR-014](ADR-014-mcp-transport-trust-and-identity.md).

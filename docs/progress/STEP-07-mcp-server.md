# Step 07: The MCP gatekeeper

Date: 2026-11-08
Phase: 1

## Goal

Expose one MCP tool, `get_jira_issue(issue_key)`, that validates its argument,
calls the backend on behalf of a named user, records what it did, and returns
the summary and acceptance criteria. Make `docker compose up` bring up the
whole stack for the first time.

## What was done

Created:

- `mcp_server/config.py` — `McpSettings`. Deliberately **not**
  `app.core.config.Settings`, which would require `DATABASE_URL`.
- `mcp_server/schemas.py` — `GetJiraIssueArgs`, `extra="forbid"`.
- `mcp_server/auth.py` — actor resolution, with a different trust model per
  transport, and a startup check.
- `mcp_server/backend_client.py` — the one thing this process talks to, plus
  the translation from backend status codes into messages a model can act on.
- `mcp_server/audit.py` — recording every tool call through the backend.
- `mcp_server/server.py` — the tool, and transport selection.
- `app/modules/audit/router.py` — `POST /api/v1/audit`, so the gatekeeper can
  record without database credentials.
- `.vscode/mcp.json` — Copilot Chat registration for local stdio use, with the
  token and actor prompted for rather than written down.
- `docs/decisions/ADR-014-mcp-transport-trust-and-identity.md`
- `docs/decisions/ADR-015-mcp-sdk-version.md`

Changed:

- `app/core/logging.py` — `configure_logging` gained a `stream` parameter. The
  MCP server must log to stderr; see below.
- `app/modules/audit/schemas.py` — `AuditWriteRequest`, `AuditWriteResponse`,
  and the `mcp.` action namespace.
- `app/modules/audit/repository.py` — `resolve_actor_user_id`.
- `app/main.py` — included the audit router.
- `docker-compose.yml` — the `mcp` service no longer uses `env_file`. See
  Security notes; this is the most important change in the file.
- `pyproject.toml` — `mcp>=2.3.0`.
- `docs/api.md`, `docs/mcp-tools.md`, `README.md`, `CHANGELOG.md`.

## How it works

### The tool, in order

`get_jira_issue` does five things, and the order is the interesting part:

1. **Mint a request id.** One per tool call, forwarded to the backend as
   `X-Request-ID` so this call, the backend's log lines and *both* audit rows
   share one identifier.
2. **Resolve the actor.** Before the argument is even looked at. A caller who
   cannot be identified is refused outright, and not audited — there is no
   actor to attribute a record to, and the backend rejects a row without one.
3. **Validate the argument.** `^[A-Z][A-Z0-9]+-[0-9]+$`. A rejection is
   audited and no network call is made.
4. **Call the backend.** With the service token, the actor header and the
   request id.
5. **Record the outcome.** Always, whatever happened.

### Identity: two transports, two trust models

This is the security-critical part, and ADR-014 has the full argument.

**stdio.** Copilot Chat launches this process as the developer and speaks to it
over the process's own pipes. The process boundary *is* the identity, so the
actor comes from `QA_ACTOR_EMAIL`. The server refuses to **start** without it,
rather than failing on the first tool call where the error would surface inside
Copilot Chat as something unexplained.

**http.** One process, many callers. The SDK exposes per-request headers via
`Context.headers`, which is `None` on stdio — a convenient discriminator. But
its own docstring is worth quoting: *"Headers are client-supplied input — never
treat one as an identity assertion."* So the HTTP transport requires the shared
service token first, compared with `hmac.compare_digest`, and only then reads
`X-Actor-Email`. The token is what makes the actor header a claim from a
trusted peer rather than from anyone who can reach the socket.

**`QA_ACTOR_EMAIL` is ignored entirely on http.** Not a fallback, not a
default. Using it would mean a container that starts cleanly and attributes
every call in the audit trail to one person — which looks like it is working,
and is the worst available outcome.

### Why the MCP server has its own settings class

Reusing `app.core.config.Settings` would have been less code. It would also
mean this process requires, and can read, `DATABASE_URL` and
`JIRA_API_TOKEN`. The entire argument for running the gatekeeper as a separate
process is that compromising it should not reach the data; a settings class
that demands the database password discards that on its first line.

`McpSettings` declares seven things: log level, transport, host, port, backend
URL, backend timeout, service token, and the stdio actor. In the container the
separation is enforced rather than merely declared — see Security notes.

### Logging to stderr, not stdout

On the stdio transport **stdout is the protocol channel**. A single log line
written there corrupts the stream, and the failure is silent and baffling:
Copilot Chat simply reports that the server did not respond. `configure_logging`
therefore gained a `stream` parameter, and `main()` passes `sys.stderr`.

### Two audit rows per call

The tool records `mcp.get_jira_issue` with `source=mcp`; the backend records
`jira_issue.read` with `source=api`. Two events at two trust boundaries, and
they share a `request_id`. Observed live:

```
    at    |       action       | outcome | source | resource_id |                        detail                         |   req
----------+--------------------+---------+--------+-------------+-------------------------------------------------------+----------
 12:44:44 | jira_issue.read    | error   | api    | ABC-1       | {"reason": "upstream_error"}                          | 9e8ed46a
 12:44:44 | mcp.get_jira_issue | error   | mcp    | ABC-1       | {"reason": "backend_refused", "project_key": "ABC"}   | 9e8ed46a
 12:44:44 | jira_issue.read    | denied  | api    | DEF-1       | {"reason": "no_project_access", "project_key": "DEF"} | 9f1ef296
 12:44:44 | mcp.get_jira_issue | error   | mcp    | DEF-1       | {"reason": "backend_refused", "project_key": "DEF"}   | 9f1ef296
 12:44:44 | mcp.get_jira_issue | denied  | mcp    |             | {"reason": "issue_key:string_pattern_mismatch"}       | 4a82de69
```

Note the last row: a rejected argument produces an `mcp` row with **no** `api`
row, because it never reached the backend. That is exactly the signal a
gatekeeper exists to provide — a run of them from one actor is someone probing.
Anyone counting reads must filter on `source` or they will double-count.

### The tool description is written for a model, not a developer

It states the argument format, that lowercase keys and URLs are rejected, that
a not-found result may mean "no access" and should not be retried, and what the
tool does *not* return. A model that guesses an argument format produces a
confident error instead of an answer.

## How to run / verify

Commands are bare; interactive `zsh` does not treat `#` as a comment unless
`interactive_comments` is set.

Bring up everything. This is the first step where that works:

```
docker compose up -d
docker compose ps
```

Expect four services, all `Up (healthy)`:

```
SERVICE    STATUS                    PORTS
api        Up 19 minutes (healthy)   127.0.0.1:8000->8000/tcp
mcp        Up 7 seconds (healthy)    127.0.0.1:8765->8765/tcp
postgres   Up 9 hours (healthy)      127.0.0.1:5433->5432/tcp
redis      Up 2 hours (healthy)      127.0.0.1:6380->6379/tcp
```

Confirm the gatekeeper holds no database or Jira credentials. Do not read the
Compose file for this; read what Compose actually resolved:

```
docker compose config
```

The `mcp` service's `environment` block should list exactly seven keys —
`BACKEND_BASE_URL`, `BACKEND_TIMEOUT_SECONDS`, `INTERNAL_API_TOKEN`,
`LOG_LEVEL`, `MCP_HOST`, `MCP_PORT`, `MCP_TRANSPORT` — and no `DATABASE_URL`,
`POSTGRES_PASSWORD` or `JIRA_API_TOKEN`.

Check the startup line:

```
docker compose logs mcp --no-log-prefix
```

Expect, among the uvicorn lines:

```json
{"level": "INFO", "service": "qa-platform-mcp", "logger": "mcp.server", "message": "mcp server starting", "version": "0.1.0", "transport": "http", "backend_base_url": "http://api:8000", "actor_source": "request header"}
```

`actor_source` is the field to look at. On http it must read `request header`.

Seed a user, or nothing will be authorised:

```
docker compose exec api python -m app.cli add-user --email you@example.com --name "Your Name" --role qa_engineer
docker compose exec api python -m app.cli grant-access --email you@example.com --project ABC
```

**Copilot Chat (the real target).** `.vscode/mcp.json` registers a stdio
instance. VS Code prompts for the token and your email and keeps both in its
secret storage. Open Copilot Chat, switch to **Agent mode**, and ask:

> what are the acceptance criteria for ABC-123?

With `JIRA_BASE_URL` still a placeholder the tool answers that the platform
could not reach Jira — which proves the whole chain: Copilot Chat → stdio MCP
server → argument validation → backend → authorisation → Jira call. Point
`JIRA_BASE_URL`, `JIRA_SERVICE_EMAIL` and `JIRA_API_TOKEN` at a real tenant to
get the issue back.

**Checking the stdio server by hand**, without VS Code:

```
MCP_TRANSPORT=stdio QA_ACTOR_EMAIL=you@example.com uv run python -m mcp_server.server
```

It should log `"transport": "stdio"` to **stderr** and then wait. Nothing may
appear on stdout; stdout is the protocol. Unset `QA_ACTOR_EMAIL` and it should
refuse to start with a message naming the variable, exiting 2.

Lint, format and tests:

```
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Expect `All checks passed!`, `88 files already formatted`, `no tests ran`.

Behaviour was verified by two throwaway scripts: 79 assertions in-process
against a mocked backend (identity for both transports, the tool contract,
eight malformed keys, six backend refusal codes, unreachable and timing-out
backends, fail-closed auditing, secret hygiene), and 16 against the **running
container** over real HTTP (initialize, tools/list, token and actor
enforcement per request, an authorised call reaching Jira, an unauthorised
project, a malformed key). See Known issues — neither is a committed test.

## Decisions and trade-offs

**`mcp` 2.x, where FastMCP is called `MCPServer`.** ADR-015. The brief names
FastMCP, which was the 1.x name; `mcp>=1.2.0` resolved to 2.3.0 and
`mcp.server.fastmcp` no longer exists. Pinning `mcp<2` would have matched the
brief literally and started the project on a superseded major version.

**Failures raise `ToolError`, not `ValueError`.** `ToolError` is the only
exception class whose message the SDK forwards to the model. This was found by
testing, not by reading: see Errors found below.

**The issue-key regex is not on the tool's input schema.** It would be one
keyword argument, and the SDK would reject a bad key before the handler runs —
outside it, so **no audit record would be written**. A run of malformed calls
from one actor is exactly what the audit trail is for, so validation happens in
the handler where the outcome can be recorded. The schema still carries a
description and examples, so the cost is a slightly thinner JSON schema.

**The HTTP transport requires a token as well as the actor header.** ADR-014.
The SDK warns that headers are client-supplied; without the token, anyone
reaching port 8765 could name any colleague.

**`QA_ACTOR_EMAIL` is not passed to the container at all.** Stronger than
ignoring it in code, and it means the Compose file states the intent.

**The gatekeeper has its own settings class and no `env_file`.** Discussed
above and in Security notes.

**`POST /api/v1/audit` rather than giving the MCP server a database
connection.** The caller supplies only what happened; the actor, source,
timestamp and request id are set by the server, and the action must sit in the
`mcp.` namespace. A gatekeeper whose compromise reaches the database is not
much of a gatekeeper.

**The audit endpoint returns no audit id.** ADR-008 chose a sequential key for
that table because nothing external quotes it. Returning one would make that
false and would leak how much work the platform has done.

**The tool returns eight fields, not everything the backend has.**
`description` and `fetched_at` are dropped: the brief asks for summary and
acceptance criteria, and a chat reply is not improved by a wall of
reproduction steps.

## Security notes

- **The gatekeeper holds no database or Jira credentials.** The `mcp` service
  has no `env_file`; its seven variables are listed one at a time. Verified by
  parsing the output of `docker compose config` — not by reading the Compose
  file — and confirming that `DATABASE_URL`, `POSTGRES_PASSWORD`,
  `POSTGRES_USER`, `JIRA_API_TOKEN`, `JIRA_SERVICE_EMAIL` and `REDIS_URL` are
  all absent. This was **not** true when the service was first written; it used
  `env_file: .env`, which injects every variable in that file. The docstring in
  `mcp_server/config.py` claimed the separation before the Compose file
  delivered it, and the docstring has been corrected to describe what is
  actually enforced and what is not: on the host, over stdio, the process does
  share `.env` for developer convenience.
- **`.env` is not in the image.** `.dockerignore` excludes it; verified by
  listing `/srv/app` inside the built image.
- **The actor header is not trusted on its own.** On http the service token is
  verified first, with `hmac.compare_digest`. A valid prefix is rejected;
  asserted.
- **`QA_ACTOR_EMAIL` cannot silently mis-attribute HTTP calls.** It is ignored
  in code, warned about at startup if set, and not passed to the container.
  Asserted that an http server with a *different* `QA_ACTOR_EMAIL` still
  resolves the actor from the header.
- **No secret reaches a log line or a response.** The service token and the
  Jira token appear in no log line across all 79 in-process assertions, and no
  `Bearer ` or `Basic ` string appears anywhere in the logs. A rejected token
  is logged as `mcp caller token rejected` with neither the token nor its
  length — the length narrows a guess.
- **Rejected arguments are not stored verbatim.** The audit row records the
  rule (`issue_key:string_pattern_mismatch`), never the value. Asserted that
  `ABC-1;DROP TABLE users` appears nowhere in the audit payloads.
- **A refusal does not reveal whether a project exists.** The 404 message
  covers both "no such issue" and "no access", preserving ADR-010 through the
  MCP layer. Asserted that the response for `DEF-1` contains no mention of
  project DEF.
- **`.vscode/mcp.json` contains no secret.** Both the token and the actor email
  are `${input:...}` placeholders, so VS Code prompts and stores them in its
  own secret storage. Asserted programmatically.
- **The audit endpoint cannot be used to forge records.** Actor, source,
  timestamp and request id are server-set, and `action` is confined to the
  `mcp.` namespace so a caller cannot write a convincing `jira_issue.read`
  row. The request body is asserted to carry none of those four fields.
- **`detail` accepts scalars only,** so a payload cannot be smuggled into the
  audit trail. Unchanged from the Step 6 addendum, now enforced across the
  HTTP boundary too by `AuditWriteRequest`.
- **Residual risk, stated plainly:** whoever holds `INTERNAL_API_TOKEN` can
  claim to be any registered user, on either transport. That is ADR-003's
  model and ADR-014 accepts it for Phase 1. It should be revisited — OAuth,
  which this SDK supports — before the HTTP port is exposed beyond loopback.

## Errors found while building this step

Three bugs, all found by running the thing rather than by reading it. Recorded
because each fails in a way that does not look like its cause.

**1. Every user-facing error message was being discarded.** The tool raised
`ValueError(message)`. The SDK forwards a message to the model only for
`ToolError`; anything else is treated as a crash, so the model received
`Error executing tool get_jira_issue` and the server logged a traceback at
ERROR. All the care taken over "the QA platform could not reach Jira" went
nowhere, and expected refusals were misfiled as crashes. Fixed by raising
`ToolError` at all three sites.

**2. `httpx.codes.UNPROCESSABLE_CONTENT` does not exist.** httpx still calls
422 `UNPROCESSABLE_ENTITY`; Starlette has renamed it to `_CONTENT`. The same
divergence was hit from the other direction in Step 6, when Starlette
deprecated the old name. Because the attribute is read inside a function, this
raised `AttributeError` only on the code paths that reached that line — so
404, 403 and 401 worked while 429, 502 and 504 crashed.

**3. The audit trail depended on the rest of the function being correct.** Bug
2 escaped as `AttributeError`, which `except BackendError` did not catch, so
those calls produced **no audit record at all** — three rows where six were
expected. The bug is fixed, but the fragility mattered more: there is now a
broad `except Exception` that records an `internal_error` row and re-raises
unchanged, so a future bug in this process still leaves a trace. The
re-raise keeps the SDK's crash classification, which is right — an internal
fault is not something a model can act on.

## Known issues / TODO

- **Still no committed tests,** now across five steps and 319 throwaway
  assertions. This step's highest-value targets: `mcp_server/auth.py` (the
  identity rules are the security boundary and have no regression test), and
  the `ToolError` requirement, which nothing would catch if someone
  reintroduced `ValueError`.
- **The service token authorises any actor claim.** Whoever holds it can
  impersonate any registered user. ADR-014 records this; it is the main
  residual risk in Phase 1 and the reason to revisit OAuth before exposing the
  HTTP port.
- **The HTTP transport has no per-caller rate limit,** so one holder of the
  token can drive backend and Jira traffic freely.
- **uvicorn's own startup lines are plain text, not JSON.** `MCPServer`
  configures uvicorn itself when serving HTTP, so three lines
  (`INFO: Started server process`, and so on) bypass the JSON formatter. Only
  a log-aggregation annoyance, but it means "every line is JSON" is not quite
  true for the mcp container.
- **`get_jira_issue` is the only tool,** and `force_refresh` is implemented in
  the backend service but exposed nowhere.
- **No negative caching,** unchanged from Step 6: a repeated bad issue key
  reaches Jira every time.
- **The stdio server shares `.env` with the backend on the host,** so the
  variables are present in its environment even though nothing in
  `mcp_server` reads them. Only the container enforces the separation. A
  separate `.env.mcp` would close this, at the cost of a second file to keep
  in sync.
- **No mypy.** Carried over from Step 3.

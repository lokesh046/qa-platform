# Step 03: app/core — settings, database, logging, errors, security

Date: 2026-11-08
Phase: 1

## Goal

Build the five cross-cutting pieces every later module depends on: validated
settings from the environment, an async database session, JSON logging with a
request id, the standard `{data, error, request_id}` response envelope, and the
internal authentication check.

## What was done

### Files created

| File | Why |
| --- | --- |
| `app/core/config.py` | One `Settings` class. The only place the environment is read. |
| `app/core/database.py` | Async engine, session factory, `get_session` dependency, declarative `Base`. |
| `app/core/logging.py` | JSON formatter, `request_id` ContextVar, request-id ASGI middleware. |
| `app/core/errors.py` | Response envelope, `AppError` hierarchy, four exception handlers. |
| `app/core/security.py` | Constant-time service-token check, then actor resolution. |
| `docs/decisions/ADR-006-async-sqlalchemy.md` | Why the backend is async. |

### Files changed

- `pyproject.toml` — `sqlalchemy` became `sqlalchemy[asyncio]` (pulls in
  `greenlet`); added `email-validator` for `EmailStr`.
- `.env.example` and `.env` — added `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`,
  `DB_POOL_TIMEOUT_SECONDS`, `DB_ECHO`; documented the required
  `postgresql+psycopg` scheme; added a hint for verifying that a published
  Docker port actually took effect.
- `.env` (local only) — `POSTGRES_HOST_PORT=5433` and `DATABASE_URL` port 5433,
  because another project's container owns 5432 on this machine.
- `docs/progress/STEP-02-docker-compose.md` — a correction; see below.
- `README.md`, `CHANGELOG.md`, `docs/api.md`.

## How it works

### config.py — one door to the environment

Everything configurable is a field on one `Settings` class, and nothing else in
the codebase calls `os.environ`. When you want to know where a value comes from
or what constrains it, there is one file to read.

Three ideas are worth understanding:

**Secrets are `SecretStr`.** `DATABASE_URL`, `JIRA_API_TOKEN` and
`INTERNAL_API_TOKEN` are typed `SecretStr`, so `repr()` and `str()` print
`**********`. A stray `logger.info(settings)` or a traceback that happens to
include the settings object cannot leak them. You call `.get_secret_value()` at
the point of use and nowhere earlier. For the times you genuinely need to show
the database location — a startup log, a readiness response, a connection error
— `database_url_safe` returns the URL with the password replaced by `***`.

**Validation happens at startup, not at first use.** A bad `API_V1_PREFIX`
raises while the process is booting rather than producing `//issues` routes.
The `DATABASE_URL` validator insists on the exact `postgresql+psycopg` scheme,
because a bare `postgresql://` URL makes SQLAlchemy reach for psycopg2, which
is not installed and cannot drive an async engine — a failure that otherwise
surfaces as an obscure import error inside the dialect registry.

**Deployed environments cannot run on placeholders.** `placeholder_secret_fields()`
reports any secret still containing `replace-with`, `change-me` or
`your-tenant`. Locally that list is informational, and Step 4 logs it at
startup so you can see that the Jira token is not real yet. When
`APP_ENV` is `staging` or `production` the same check becomes a hard failure,
along with a 32-character minimum on `INTERNAL_API_TOKEN` and a refusal to run
with `DB_ECHO=true` (which would write bound parameters, and therefore personal
data, to the log).

This is the one Pydantic model in the project that does **not** set
`extra="forbid"`. The process environment legitimately contains hundreds of
unrelated variables, and the MCP server's own `MCP_*` and `BACKEND_*` settings
share the same `.env` file. Request models all forbid extras; settings must
ignore them.

### database.py — async sessions, created lazily

The engine lives behind an `lru_cache`, so importing this module opens no
socket. That keeps unit tests fast and means a misconfigured database fails on
the first request rather than at interpreter startup.

`get_session` is the FastAPI dependency. It yields a session, rolls back if the
handler raises, and always closes. It deliberately does **not** commit:
committing is the service layer's job, because only the service knows where one
unit of work ends.

Two settings deserve explanation. `expire_on_commit=False` stops SQLAlchemy
invalidating every attribute on commit; with the default, reading a field after
a commit triggers a refresh, and in async code that refresh raises
`MissingGreenlet` instead of quietly issuing a SELECT. `pool_pre_ping=True`
spends one cheap round trip per checkout and in exchange survives Postgres
closing an idle connection, which otherwise appears as a random
`OperationalError` on an unrelated request.

`Base` lives here rather than in `app/models/` because the metadata is
infrastructure: Alembic needs it, and a model package that imported the engine
would create an import cycle. Step 5's models inherit from it.

Why async at all is ADR-006. The short version: this service almost only waits
on the network, and the Jira client is already async.

### logging.py — one JSON object per line, and the thread that ties them together

`configure_logging` replaces the root handler with one that writes a single JSON
object per line to stdout. Container runtimes collect stdout, so there is no
file to rotate and no path to configure. Anything passed as `extra={...}`
becomes a top-level field, which is what makes structured queries possible:

```python
logger.info("jira fetch", extra={"issue_key": "ABC-1", "cached": True})
```

`RequestIdMiddleware` gives every request an id, publishes it in a `ContextVar`,
and echoes it in the `X-Request-ID` response header. The formatter reads that
ContextVar, so **every** log line emitted while handling a request carries the
id without anyone threading it through function signatures. A user reports a
failure, quotes the id from the error response, and one query returns every line
for that request.

The middleware is written as raw ASGI rather than using Starlette's
`BaseHTTPMiddleware`. That is not stylistic: `BaseHTTPMiddleware` runs the
downstream app in a separate task, which breaks ContextVar propagation in
exactly the way that would make the request id vanish from log lines, and it
buffers streaming responses.

An inbound `X-Request-ID` is reused so a trace can span services, but only after
sanitising. The header is attacker-controlled and goes into both our logs and a
response header, so embedded newlines would allow log injection and control
characters would allow header splitting. The rule is narrow — printable
alphanumerics plus `-_.`, at most 64 characters — and anything else is discarded
in favour of a fresh id.

**What is never logged:** request bodies, response bodies, headers, and query
strings. Bodies carry Jira content and later test data; `Authorization` carries
the service token; query strings are the easiest place for a secret to end up by
accident. Each request produces one line with method, path, status and duration,
which is enough to find a slow or failing endpoint and not enough to leak
anything. `uvicorn.access` is disabled entirely because this line is better.

### errors.py — one response shape, raised not returned

Every response has three keys:

```json
{"data": {...}, "error": null, "request_id": "9bfa3b8b..."}
```

On failure `data` is `null` and `error` is `{"code", "message", "details"}`. A
client never has to inspect the shape of a response to find out whether it
worked, and every failure carries the id needed to find the matching log lines.

Failing is done by raising. A service raises `NotFoundError("Issue ABC-1 not
found.")`; the registered handler turns it into a 404 with code `not_found` in
the envelope. Services never build an HTTP response and routers never translate
exceptions by hand. `ErrorCode` is a `StrEnum` of stable identifiers — clients
branch on them, so treat them as API surface: add freely, rename never.

Four handlers are registered, and the fourth is the important one. The catch-all
for `Exception` logs the traceback in full, with the request id attached, and
returns a fixed message:

> An internal error occurred. Quote the request_id when reporting it.

The caller learns nothing about our internals. Exception text routinely contains
table names, file paths, SQL fragments and occasionally connection strings; a
500 is exactly the moment not to hand those out. The verification below asserts
this directly: an endpoint that raises `RuntimeError("secret internal detail:
table users, password hunter2")` returns none of that string, while the log
contains all of it.

The validation handler similarly keeps FastAPI's field errors, which are useful,
but drops each error's `input` member, which echoes the caller's value — for a
malformed auth payload that would mean echoing a credential into the response.

`ResponseEnvelope[T]` is a PEP 695 generic, so OpenAPI documents the real
payload type: `ResponseEnvelope[IssueRead]` renders `data` as an `IssueRead`
rather than as `any`.

### security.py — token first, then identity

One dependency protects an endpoint:

```python
async def read_issue(actor: Annotated[Actor, Depends(require_actor)]) -> ...:
```

It verifies the `Authorization: Bearer <token>` header against
`INTERNAL_API_TOKEN`, and only then parses `X-Actor-Email` into an `Actor`.

Two things are load-bearing. The token is compared with
`hmac.compare_digest`, never `==`: a plain comparison returns on the first
differing byte, which leaks the token's prefix through response timing. And the
order of the two checks is itself a security property — reading the actor header
first would let an unauthenticated caller choose whose name ends up in the audit
log.

`Actor` carries identity only. Whether that identity may see a given Jira
project is a separate question, answered in Step 6 against
`user_project_access`. Keeping them apart means authentication cannot
accidentally imply authorisation.

ADR-003 records the limitation honestly: anyone holding `INTERNAL_API_TOKEN` can
claim to be any user, so the token is high-value and this design holds only
while it stays on trusted machines.

## How to run / verify

Code blocks contain bare commands only. Zsh does not treat `#` as a comment
interactively unless you run `setopt interactive_comments`.

### Prerequisites

```bash
docker compose up -d postgres redis
docker inspect qa-platform-postgres-1 --format '{{json .NetworkSettings.Ports}}'
```

The second command must show a non-empty host binding. An empty list means the
port was not published and a host connection will reach something else entirely
— see the Step 2 correction.

### Lint and import

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run python -c "import app.core.config, app.core.database, app.core.logging, app.core.errors, app.core.security; print('core imports')"
```

Observed `All checks passed!`, `48 files already formatted`, and
`core imports`.

### Behavioural verification

A 73-assertion harness was written and run against the live Postgres. It lives
at:

```
/private/tmp/claude-501/-Users-yeswanth-Documents-jira-mcp/ec87d46b-9244-406c-9c11-fb72519bbde4/scratchpad/verify_core.py
```

```bash
PYTHONPATH=/Users/yeswanth/Documents/jira_mcp uv run python /private/tmp/claude-501/-Users-yeswanth-Documents-jira-mcp/ec87d46b-9244-406c-9c11-fb72519bbde4/scratchpad/verify_core.py
```

Final line: `PASSED 73   FAILED 0`. What it establishes, by group:

**config (18)** — loads from `.env`; secrets absent from `repr()` and `str()`;
`database_url_safe` masks the password; rejects a non-`psycopg` scheme, a prefix
without a leading slash, a prefix with a trailing slash, an `http` Jira URL, and
a non-redis Redis URL; strips a trailing slash from the Jira URL; tolerates
placeholders when `APP_ENV=local`; rejects placeholders, a short
`INTERNAL_API_TOKEN` and `DB_ECHO=true` when `APP_ENV=production`; accepts a
clean production config; `placeholder_secret_fields()` names the offenders;
`get_settings()` is cached.

**logging (7)** — the formatter emits parseable JSON with
`timestamp`/`level`/`service`/`logger`/`message`; `extra` fields are merged;
timestamps are UTC ISO-8601 ending in `Z`; no `request_id` outside a request;
exceptions are captured as a formatted traceback field.

**errors and middleware (25)** — the success envelope has exactly
`{data, error, request_id}`; `X-Request-ID` is echoed and matches the body; a
safe inbound id is reused; a malformed one (`"bad\nvalue with spaces"`) and an
overlong one (200 chars) are both replaced; an `AppError` maps to its declared
status, code, message and details; an unhandled exception returns 500 with
`internal_error` and **leaks none** of `hunter2`, `table users`, `RuntimeError`
or `Traceback`, while the log contains all of them; a log line carries the same
`request_id` as the response; the access line has method, path, status and
duration and no `authorization`; a 422 uses the envelope, lists field errors,
and does not echo the offending input value; Starlette's own 404 and 405 are
wrapped in the envelope too.

**security (17)** — the correct token is accepted; a wrong token, and the
correct token with its last character removed, are both rejected; a valid actor
parses and is trimmed; a missing or malformed actor is rejected; `Actor` is
frozen; through a real app, no auth gives 401, a token without an actor gives
401, a bad token with a valid actor gives 401, a non-Bearer scheme gives 401,
and token plus actor gives 200 with the actor resolved; **the token appears in
no log line and no response body**.

**database (2)** — the engine builds, `SELECT 1` succeeds against the live
Postgres 17.11, the engine is cached, and `Base.metadata` exists for Alembic.

**envelope (2)** — `ResponseEnvelope[T]` is generic in the OpenAPI schema;
`ErrorCode` values are the expected strings.

A sample log line, as actually emitted:

```json
{"timestamp": "2026-11-08T04:09:40.024Z", "level": "INFO", "service": "qa-platform-verify", "logger": "app.request", "message": "request", "request_id": "69b92474ba3446e1a9eb4c8b226bcc70", "http_method": "GET", "http_path": "/whoami", "http_status": 200, "duration_ms": 0.41}
```

## Decisions and trade-offs

**ADR-006** is the significant one: the backend is async, driven by psycopg3,
with Alembic kept blocking.

Smaller choices:

- **`SecretStr` for secrets rather than plain `str`.** Costs a
  `.get_secret_value()` call at each use site. Buys immunity from the most
  common way credentials leak, which is an object printed into a log.
- **Placeholder detection is advisory locally and fatal in deployments.** A
  hard failure everywhere would make `cp .env.example .env` useless; no check at
  all means a placeholder reaches production. Gating on `APP_ENV` gets both.
- **Raw ASGI middleware instead of `BaseHTTPMiddleware`.** More code, and the
  only option that preserves the ContextVar the whole design depends on.
- **`ValidationFailedError`, not `ValidationError`.** Pydantic owns that name,
  and two different `ValidationError`s in one import block is a bug waiting to
  happen.
- **Stdlib logging with a custom formatter rather than structlog.** About eighty
  lines, no new dependency, and third-party loggers fold into the same handler
  by propagation. Revisit if log enrichment spreads beyond `extra=`.
- **`get_session` does not commit.** A dependency that auto-commits makes every
  handler a transaction boundary, which is wrong as soon as one handler needs
  two units of work or none.
- **The envelope is built by helper functions returning `dict`, not by
  constructing `ResponseEnvelope`.** The model exists for OpenAPI; serialising
  through it on every response would cost a second validation pass for no gain.

## Security notes

- **No secret can be printed by accident.** Secrets are `SecretStr`; the
  verification asserts that neither `repr()` nor `str()` of the settings object
  contains the database password, and that the service token appears in no log
  line and no response body.
- **Constant-time token comparison.** `hmac.compare_digest`, with both sides
  encoded to bytes so a non-ASCII token raises nothing and a 401 cannot become
  a 500.
- **Token verified before the actor header is read.** Otherwise an
  unauthenticated caller could choose the identity recorded in the audit log.
- **A 500 reveals nothing.** The traceback goes to the log; the response gets a
  fixed message and the request id. Asserted against a deliberately
  secret-bearing exception message.
- **A 422 does not echo input.** FastAPI's `input` member is dropped, so a
  malformed credential is not reflected back or logged.
- **Inbound `X-Request-ID` is sanitised** to printable alphanumerics plus
  `-_.`, maximum 64 characters, which closes log injection via newlines and
  header splitting via control characters.
- **No bodies, headers or query strings in logs.** Stated as a `REDACTION`
  comment block at the top of `logging.py` so the rule is visible to whoever
  edits it next.
- **`DB_ECHO=true` is refused in staging and production**, because statement
  logging includes bound parameters.
- **Connection failures are logged, not returned.** A psycopg error message
  includes the host and user, so `check_database_connection` returns a plain
  `False` and logs the detail.

## Known issues / TODO

- **These 73 assertions are not yet regression tests.** They ran once, from a
  scratchpad script that will not survive the session. Step 8's brief covers
  validation and the Jira service, not `app/core`, so this is a real gap:
  `app/core` is the most security-sensitive code in the project and currently
  has no test that would catch a regression. They should become
  `tests/unit/test_core_config.py`, `test_core_errors.py`,
  `test_core_logging.py` and `test_core_security.py`.
- **`email-validator` rejects reserved TLDs.** `svc@test.invalid`,
  `user@example.test` and `a@b.localhost` all fail validation. This bit the
  first verification run and will bite whoever writes test fixtures in Step 8.
  Use an ordinary-looking domain. Noted in the `settings_from_environ`
  docstring.
- **No mypy.** Ruff's `ANN` rules force annotations to exist but do not check
  them. There are three `# type: ignore` comments — two for
  pydantic-settings' constructor, which Pydantic cannot type when every value
  comes from the environment, and one for the `settings` default on
  `require_actor`. A type checker would confirm whether those are still needed.
- **`require_actor` has an awkward default.** `settings: Annotated[Settings,
  Depends(get_settings)] = None` with an ignore comment, because a dependency
  with no default cannot follow `Header` parameters that have one. It works and
  is ugly; a `Depends` default on the whole function or a small wrapper would be
  cleaner.
- **The connection pool is per process.** `uvicorn --workers N` multiplies
  `DB_POOL_SIZE + DB_MAX_OVERFLOW`. Four workers at the defaults demand 60
  connections against a Postgres default `max_connections` of 100. Size this
  deliberately before deploying.
- **Redis is configured but unused.** `REDIS_URL` is validated and nothing
  connects to it. Step 6 uses it for the Jira cache. There is no
  `redis_client.py` yet, and no readiness check for Redis.
- **No rate limiting.** `RateLimitedError` and `ErrorCode.RATE_LIMITED` exist so
  the vocabulary is in place; nothing raises them.
- **`app/core/__init__.py` is empty.** Imports are deep
  (`from app.core.config import get_settings`). Fine, and worth revisiting if
  the import lines get noisy.

## 3-line summary

- **What changed:** added the five `app/core` modules — `config.py` (one
  validated `Settings`, secrets as `SecretStr`, production placeholder guard),
  `database.py` (lazy async engine, `get_session`, declarative `Base`),
  `logging.py` (JSON formatter, `request_id` ContextVar, raw-ASGI middleware),
  `errors.py` (`{data, error, request_id}` envelope, `AppError` hierarchy, four
  handlers) and `security.py` (constant-time token check, then actor); switched
  to `sqlalchemy[asyncio]`, added `email-validator`, and corrected a wrong claim
  in the Step 2 log.
- **Where it is documented:** `docs/progress/STEP-03-app-core.md`,
  `docs/decisions/ADR-006-async-sqlalchemy.md`, `docs/api.md`, `README.md`,
  `CHANGELOG.md`.
- **Next step:** Step 4 — `app/main.py` with `/health` and `/ready` under
  `/api/v1`, wiring in the middleware and the exception handlers.

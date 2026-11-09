# AI QA Automation Platform

Internal platform that turns a Jira issue into reviewed, executable test
automation. Users talk to it through GitHub Copilot Chat in Agent mode; Copilot
calls our MCP server, which forwards to a FastAPI backend.

**This repository is at Phase 1.** Phase 1 delivers the backend skeleton, the
Jira read path, and one MCP tool. Later phases add BRD generation, test
generation, approvals, Playwright execution, and Jira write-back.

## Architecture

```
GitHub Copilot Chat (Agent mode)
        │  MCP, stdio
        ▼
mcp_server/            thin gatekeeper: authenticate, validate, audit, forward
        │  HTTPS + shared service token
        ▼
app/                   FastAPI backend: all business logic, organised as modules
        │
        ├── PostgreSQL  users, access, cached Jira issues, jobs, audit log
        ├── Redis        cache and job queue
        └── Jira Cloud   REST API, read-only service account
```

The MCP server holds **no business logic**. If a rule is worth writing down, it
belongs in `app/modules/<module>/service.py`.

## Setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker Desktop.

```bash
# 1. Install dependencies into .venv (uv fetches Python 3.12 if needed)
uv sync

# 2. Create your local environment file and fill in the placeholders
cp .env.example .env
```

Open `.env` and set at minimum `JIRA_BASE_URL`, `JIRA_SERVICE_EMAIL`,
`JIRA_API_TOKEN`, and `INTERNAL_API_TOKEN`. Nothing in `.env` is committed.

## Running locally

Needs Docker Desktop running. Check with `docker info` first; if it errors,
start Docker Desktop (`open -a Docker` on macOS) and wait for it to settle.

Every code block below is bare commands, safe to paste whole into zsh. (Zsh
does not treat `#` as a comment interactively unless you run
`setopt interactive_comments`, so pasting annotated commands fails oddly.)

### The whole stack

```bash
docker compose up -d
docker compose ps
```

All four services should read `healthy`.

| Service | URL | Notes |
| --- | --- | --- |
| API | http://localhost:8000/api/v1/health | Working |
| API docs | http://localhost:8000/docs | Working |
| MCP (HTTP transport) | http://localhost:8765/mcp | Working |
| Postgres | `localhost:5433` | `psql -h localhost -p 5433 -U qa -d qa_platform` |
| Redis | `localhost:6380` | `redis-cli -h localhost -p 6380 ping` |

The Postgres and Redis host ports differ from the defaults in `.env.example`
because another project on this machine already owned 5432 and 6379. They are
whatever `POSTGRES_HOST_PORT` and `REDIS_HOST_PORT` say in your `.env`.

Confirm a port was really published, rather than merely requested:

```bash
docker inspect qa-platform-api-1 --format '{{json .NetworkSettings.Ports}}'
```

Expect `{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8000"}]}`. An **empty
list** means the port was not bound and your requests are reaching something
else. `docker compose up` does not treat that as an error, and it has already
happened once on this project; see the correction in
[STEP-02](docs/progress/STEP-02-docker-compose.md).

Every port binds `127.0.0.1` only, so nothing is published to your network. The
host-side port of each service is set by `POSTGRES_HOST_PORT`,
`REDIS_HOST_PORT`, `API_HOST_PORT` and `MCP_HOST_PORT` in `.env`; change one if
something on your machine already owns the default.

There is no object-storage service. Nothing stores an artifact before Phase 3,
and MinIO's container images are no longer publicly pullable. See
[ADR-005](docs/decisions/ADR-005-object-storage-deferred.md).

Other useful commands:

```bash
docker compose logs -f api
docker compose down
docker compose down -v
```

`down` stops the stack and keeps the data. `down -v` also deletes the volumes.

### Checking the API is up

```bash
curl -s http://127.0.0.1:8000/api/v1/health
curl -s http://127.0.0.1:8000/api/v1/ready
```

`/health` is liveness: it touches no dependency and returns 200 whenever the
process can answer. `/ready` is readiness: it probes Postgres and Redis and
returns 200 with a per-dependency check list, or 503 with the same list under
`error.details`. Its `target` field reports the host each probe actually
reached, with the password masked, which is the fastest way to catch a
misdirected connection. See
[ADR-007](docs/decisions/ADR-007-health-and-readiness-semantics.md) and
[docs/api.md](docs/api.md).

### Infrastructure only

To start just Postgres and Redis, and run the API from your shell:

```bash
docker compose up -d postgres redis
docker compose ps
docker compose exec postgres psql -U qa -d qa_platform -c 'select version();'
docker compose exec redis redis-cli ping
```

Expected: both services `healthy`, a PostgreSQL 17 version string, and `PONG`.

### Without Docker

`.env` holds `localhost` hostnames, so the backend also runs directly against
the compose infrastructure:

```bash
docker compose up -d postgres redis
uv run uvicorn app.main:app --reload
```

Serves on http://127.0.0.1:8000. Note that `/ready` then reports
`localhost:5433` and `localhost:6380` as its targets, where the containerised
API reports `postgres:5432` and `redis:6379` — `docker-compose.yml` overrides
`DATABASE_URL` and `REDIS_URL` for the `api` service so it uses the compose
network.

### Database migrations

Apply the schema. Needs Postgres up and `.env` present:

```bash
uv run alembic upgrade head
uv run alembic current
```

`upgrade head` prints the target with the password masked, then one
`Running upgrade` line. `current` then reports the revision followed by
`(head)`.

Migrations are **not** run automatically when the application starts. Nothing
in `app/main.py` calls Alembic: auto-migrating on boot means every replica
races to run the same DDL, and a bad migration takes the service down instead
of failing a deploy step.

Full command set and the rules for writing a revision are in
[migrations/README.md](migrations/README.md). One of them is worth repeating
here: `alembic check` does not compare `CHECK` constraints, so a green result
does not mean the schema matches the models.

### Seeding users and project access

Nothing can be authorised until a user exists with a grant, so a fresh
database refuses every issue read — correctly, but unhelpfully. There is no
admin API in Phase 1 ([ADR-012](docs/decisions/ADR-012-operator-cli-for-seeding.md));
use the CLI:

```bash
docker compose exec api python -m app.cli add-user --email you@example.com --name "Your Name" --role qa_engineer
docker compose exec api python -m app.cli grant-access --email you@example.com --project ABC
docker compose exec api python -m app.cli list-users
```

It also has `revoke-access`, `deactivate-user` and `activate-user`. Commands
are idempotent and exit non-zero on failure. Run it on the host with
`uv run python -m app.cli ...` instead.

### Reading a Jira issue

```bash
set -a
. ./.env
set +a
curl -s -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
     -H "X-Actor-Email: you@example.com" \
     http://127.0.0.1:8000/api/v1/issues/ABC-123
```

With `JIRA_BASE_URL` still a placeholder this returns 502 "Could not reach
Jira", which means all four authorisation gates passed. Point it at a real
tenant to get a 200.

A project you hold no grant for returns **404, not 403** — the same response a
non-existent issue gives, so the endpoint cannot be used to discover which
projects exist. See
[ADR-010](docs/decisions/ADR-010-issue-read-authorisation.md).

The real reason is in the audit trail. Every read attempt is recorded, allowed
or refused:

```bash
docker compose exec -T postgres psql -U qa -d qa_platform \
  -c "select occurred_at::time(0), actor_email, outcome, resource_id, detail from audit_logs order by id desc limit 10;"
```

There is no endpoint for this; reading the trail is an administrator's job.
Audit writes are fail-closed — if one fails the request returns 500 and no
data, so watch for the `app.audit` logger at ERROR
([ADR-013](docs/decisions/ADR-013-audit-write-transaction-and-failure.md)).

### Using it from Copilot Chat

[.vscode/mcp.json](.vscode/mcp.json) registers a **stdio** MCP server that VS
Code starts itself. That is a separate instance from the `mcp` container, which
serves HTTP for remote clients.

First make sure the backend is up and you exist as a user with a grant (see
above). Then open Copilot Chat, switch to **Agent mode**, and ask:

> what are the acceptance criteria for ABC-123?

VS Code prompts once for `INTERNAL_API_TOKEN` and your work email and keeps
both in its own secret storage — neither is written into the JSON file.

With `JIRA_BASE_URL` still a placeholder the tool replies that the platform
could not reach Jira. That is the whole chain working: Copilot Chat → MCP
server → argument validation → backend → authorisation → Jira call.

Every tool call is recorded twice, by design: once by the gatekeeper
(`source=mcp`) and once by the backend (`source=api`), sharing a `request_id`.
[docs/mcp-tools.md](docs/mcp-tools.md) has the full tool reference.

To run the stdio server by hand:

```bash
MCP_TRANSPORT=stdio QA_ACTOR_EMAIL=you@example.com uv run python -m mcp_server.server
```

It logs to **stderr** and then waits. Nothing may be written to stdout: on
stdio, stdout is the protocol channel.

### Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config --quiet
```

Lint and formatting, the test suite, and compose-file validation. All four
should exit 0 and print little.

### Tests

```bash
uv run pytest                                   # the whole suite, ~1 second
uv run pytest tests/unit/test_jira_service.py -v  # one file
uv run pytest -k "acceptance_criteria"          # by name
```

**Nothing needs to be running.** The suite uses no database, no Redis, no
network and not your `.env`: Jira and the backend are mocked with `respx`, the
jira service's repository is stubbed, and the MCP tool is driven through the
SDK's in-process transport. You can prove it:

```bash
docker compose down
mv .env .env.hidden && uv run pytest; mv .env.hidden .env
```

That is a deliberate constraint, not a limitation — see
[ADR-018](docs/decisions/ADR-018-test-strategy.md). The cost is that the
database layer is not covered: `tests/integration/` is empty, and its
[README](tests/integration/README.md) lists what belongs there and the fixture
it needs first.

## Project structure

```
pyproject.toml          Dependencies and Ruff/pytest configuration
uv.lock                 Resolved dependency versions; commit it
.env.example            Every environment variable, with placeholder values
Dockerfile              Multi-stage build; targets "api" and "mcp"
.dockerignore           Keeps secrets, caches and the local venv out of builds
docker-compose.yml      Local stack: Postgres, Redis, API, MCP

mcp_server/             MCP gatekeeper. Holds no database or Jira credentials
  server.py             The get_jira_issue tool and transport selection
  config.py             Its own settings class, not the backend's; see ADR-014
  auth.py               Caller identity. Two transports, two trust models
  schemas.py            Tool argument validation
  backend_client.py     httpx client for the FastAPI backend
  audit.py              Records every tool call via POST /api/v1/audit
.vscode/mcp.json        Copilot Chat registration for the stdio server

app/
  __init__.py           __version__, reported by /api/v1/health
  main.py               FastAPI assembly: middleware, handlers, routers, lifespan
  core/                 Cross-cutting concerns
    config.py           One validated Settings class; the only reader of os.environ
    database.py         Async SQLAlchemy engine, get_session dependency, Base
    redis.py            Lazy Redis client and its readiness probe
    security.py         Constant-time service-token check, then actor resolution
    logging.py          JSON logging and the request_id middleware
    errors.py           {data, error, request_id} envelope and error hierarchy
  models/               SQLAlchemy models; import every new one in __init__.py
    base.py             Key and timestamp mixins, enum_column helper
    user.py             User, UserProjectAccess, UserRole
    jira_issue.py       JiraIssue (read-through cache)
    job.py              Job, JobType, JobStatus
    audit_log.py        AuditLog (append-only)
  cli.py                Operator CLI: seed users and project access
  modules/              One folder per capability; see app/modules/README.md
    health/             /api/v1/health and /api/v1/ready
    jira/               /api/v1/issues/{issue_key}
      client.py         httpx gateway to Jira Cloud
      adf.py            Atlassian Document Format to plain text
    audit/              Append-only audit trail; writes only, no router
  llm/prompts/          Prompt templates (Phase 2)
  workflows/            LangGraph workflows (Phase 3)

workers/sandbox/        Sandboxed Playwright runner (Phase 4)
specs/openspec/         OpenSpec specs and change proposals (Phase 2)
templates/playwright/   Playwright project skeleton (Phase 4)
alembic.ini             Alembic config; holds no database URL by design
migrations/             Alembic environment; see migrations/README.md
  env.py                Blocking engine, URL from app.core.config
  versions/             One file per revision
tests/conftest.py       Hermetic fixtures: no .env, no infrastructure
tests/unit/             Unit tests. 433 of them; need nothing running
tests/integration/      Empty; its README says what belongs there and why
docs/                   Step log, decision records, API and MCP tool reference
```

Every empty folder above contains a README explaining what will fill it.

## Environment variables

Read from the environment only. No defaults for anything secret, and no secret
ever appears in code, logs, or Git.

`.env` holds the **localhost** hostnames so the API also runs outside Docker.
`docker-compose.yml` overrides `DATABASE_URL`, `REDIS_URL` and
`BACKEND_BASE_URL` with the in-network service names, so there is only ever one
env file to maintain.

One deliberate exception: the **`mcp` service has no `env_file`**. It lists its
seven variables individually, so `DATABASE_URL`, `POSTGRES_PASSWORD` and
`JIRA_API_TOKEN` are never in the gatekeeper's environment. Adding a variable
there is a decision; adding one to `.env` is not. Check what it actually
receives with `docker compose config`, not by reading the file.

Object-storage variables are commented out in `.env.example` until Phase 3
picks a provider ([ADR-005](docs/decisions/ADR-005-object-storage-deferred.md)).

| Variable | Used by | Purpose |
| --- | --- | --- |
| `APP_ENV` | app | `local`, `ci`, `staging`, or `production`. |
| `APP_NAME` | app | Service name in logs. |
| `API_V1_PREFIX` | app | Route prefix, default `/api/v1`. |
| `LOG_LEVEL` | app, mcp | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `MCP_TRANSPORT` | mcp | `stdio` for Copilot Chat, `http` for the container. See ADR-004 and ADR-014. |
| `MCP_HOST` | mcp | Listen address when `MCP_TRANSPORT=http`. |
| `MCP_PORT` | mcp | Listen port when `MCP_TRANSPORT=http`, default `8765`. |
| `POSTGRES_USER` | compose | Database user created in the Postgres container. |
| `POSTGRES_PASSWORD` | compose | **Secret.** Password for that user. |
| `POSTGRES_DB` | compose | Database name created on first start. |
| `DATABASE_URL` | app | SQLAlchemy URL. The scheme must be exactly `postgresql+psycopg`. **Contains a secret.** Overridden in compose. |
| `DB_POOL_SIZE` | app | Pooled connections per process, default `5`. |
| `DB_MAX_OVERFLOW` | app | Extra connections above the pool, default `10`. |
| `DB_POOL_TIMEOUT_SECONDS` | app | Wait for a free connection, default `10`. |
| `DB_ECHO` | app | Log every SQL statement. Refused when `APP_ENV` is staging or production. |
| `REDIS_URL` | app | Redis connection URL. Overridden in compose. |
| `REDIS_TIMEOUT_SECONDS` | app | Connect and read timeout for Redis, default `5`. Keep it under the readiness probe's own timeout. |
| `POSTGRES_HOST_PORT` | compose | Host port mapped to Postgres, default `5432`. |
| `REDIS_HOST_PORT` | compose | Host port mapped to Redis, default `6379`. |
| `API_HOST_PORT` | compose | Host port mapped to the API, default `8000`. |
| `MCP_HOST_PORT` | compose | Host port mapped to the MCP HTTP listener, default `8765`. |
| `JIRA_BASE_URL` | app | Jira Cloud tenant, e.g. `https://acme.atlassian.net`. |
| `JIRA_SERVICE_EMAIL` | app | Service-account email used for Basic auth. |
| `JIRA_API_TOKEN` | app | **Secret.** Atlassian API token for that account. |
| `JIRA_TIMEOUT_SECONDS` | app | Per-request timeout against Jira. |
| `JIRA_CACHE_TTL_SECONDS` | app | How long a cached issue stays fresh. `0` disables the cache. |
| `JIRA_ACCEPTANCE_CRITERIA_FIELD` | app | Optional. Your tenant's custom field id, e.g. `customfield_10040`. Unset means fall back to an "Acceptance Criteria" heading in the description. |
| `INTERNAL_API_TOKEN` | app, mcp | **Secret.** Shared token the MCP server presents to the backend. |
| `BACKEND_BASE_URL` | mcp | Backend base URL as the MCP server sees it. Overridden in compose. |
| `BACKEND_TIMEOUT_SECONDS` | mcp | Per-request timeout against the backend. |
| `QA_ACTOR_EMAIL` | mcp | Your work email, used as the actor **only** on the stdio transport. Ignored on `http`, where each request carries its own `X-Actor-Email`, and not passed to the `mcp` container at all. See ADR-014. |

## Documentation

- `docs/progress/` — one file per step: what changed, how it works, how to verify.
- `docs/decisions/` — architecture decision records.
- `docs/api.md` — every HTTP endpoint.
- `docs/mcp-tools.md` — every MCP tool.
- `CHANGELOG.md` — one dated line per step, newest first.

## Conventions

- Type hints everywhere. `uv run ruff check .` must be clean.
- Pydantic input models reject unknown fields (`extra="forbid"`). `Settings` is
  the one exception: it ignores unknown environment variables.
- Secrets are `SecretStr`, so they cannot be printed by accident. Call
  `.get_secret_value()` at the point of use.
- Never log a token, a password, a header, a request body, or a query string.
  Each request logs method, path, status and duration, and nothing else.
- Fail by raising an `AppError` subclass. Handlers in `app/core/errors.py` turn
  it into the right status and envelope. Services never build HTTP responses.
- A 500 response carries a fixed message and the `request_id`. The traceback
  goes to the log, never to the caller.
- Secrets come from the environment, never from code or a committed file.

## Version control

The repository tracks code, configuration templates and documentation. It does
not track secrets: `.gitignore` excludes `.env` and every `.env.*` variant,
with `!.env.example` as the single exception, so the template is versioned and
no filled-in copy ever can be.

Before any commit that touches configuration, check what is actually staged:

```bash
git diff --cached --name-only | grep -E '^\.env'   # expect only .env.example
git diff --cached | grep -c 'ATATT'                 # expect 0 (Atlassian tokens)
```

A credential that reaches a commit is published even if the next commit removes
it, because the object stays in the history and on any clone. Rotating the
credential is then the only real fix.

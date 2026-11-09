# Changelog

One line per step, newest first.

- 2026-11-09 — Step 09: OpenSpec installed and its role settled. `openspec
  init --tools claude,github-copilot` created `openspec/` plus 24 agent
  instruction files, and `specs/openspec/` was deleted as a duplicate of it.
  Investigation first, because it changed the design: OpenSpec is a workflow
  for AI coding agents with no `generate` command and no LLM dependency, so it
  cannot turn a ticket into requirements — a model does that, and the CLI only
  manages the files. It is therefore a development and CI dependency, never
  called from an HTTP request, and Node stays out of the Docker image
  (ADR-019). ADR-020 settles Phase 2's model: `claude-sonnet-5-5` as a
  configured default, prompts as versioned files, the provider mocked in tests.
  No application code changed.

- 2026-11-09 — Version control and first real Jira connection. The repository
  was initialised (`git init -b main`) after eight steps of untracked work, and
  Phase 1 was committed as one commit; `.gitignore` already excluded `.env` and
  `.env.*`, and the staged tree was audited for live credentials before the
  commit. `JIRA_BASE_URL`, `JIRA_SERVICE_EMAIL` and `JIRA_API_TOKEN` were
  pointed at a real Jira Cloud tenant, so the jira module read live issues for
  the first time: cache miss then hit, a refusal for an ungranted project, and
  `allowed` / `denied` / `error` rows in `audit_logs` from both the `api` and
  `mcp` sources. `POSTGRES_PASSWORD` and `INTERNAL_API_TOKEN` were rotated off
  the values published in `.env.example`, which the startup placeholder warning
  had been reporting since Step 4 and nobody had read.

- 2026-11-08 — Step 08: the committed test suite. 433 assertions in
  `tests/unit`, running with no database, no Redis, no network and no `.env`,
  so `uv run pytest` passes on a clean checkout with nothing started — the gap
  flagged at the end of every step since Step 3, where 319 assertions existed
  only as scratch scripts against the dev database. Covers the ADF parser, the
  issue-key pattern in both validators, the jira service's four gates (the stub
  repository records its call order, so "no Jira call before the access check"
  is an assertion), the Jira and backend HTTP clients per status, both MCP
  transports' trust models, and the tool driven through the SDK's in-process
  client. Two security fixes found by the tests: a configuration error printed
  the credential it rejected, because `str(ValidationError)` appends
  `input_value` — including `DATABASE_URL` with its password, into the
  container log at startup (ADR-016); and an actor's email was recorded with
  its local-part casing intact, so one person could appear in the audit trail
  as several actors (ADR-017). Added ADR-018 on the test strategy.

- 2026-11-08 — Step 07: the MCP gatekeeper. `mcp_server/` with one tool,
  `get_jira_issue(issue_key)`: validate, resolve the actor, call the backend,
  audit, return summary and acceptance criteria. Adds `POST /api/v1/audit` so
  the gatekeeper records without database credentials, `.vscode/mcp.json` for
  Copilot Chat over stdio, and a `stream` argument to `configure_logging`
  because on stdio stdout is the protocol channel. `docker compose up` brings
  up all four services for the first time. The `mcp` service no longer uses
  `env_file`, so it receives no database or Jira credentials — verified from
  the resolved Compose config. Added ADR-014 (two transports, two trust
  models; http needs a token as well as the actor header) and ADR-015 (`mcp`
  2.x renamed FastMCP to MCPServer). Three bugs found by running it: every
  user-facing message was discarded because only `ToolError` is forwarded to
  the model, `httpx.codes.UNPROCESSABLE_CONTENT` does not exist, and the
  resulting `AttributeError` skipped the audit write entirely. 79 in-process
  plus 16 live-container assertions passed.

- 2026-11-08 — Step 06 addendum: audit records at the API layer. Adds
  `app/modules/audit/` (`schemas`, `repository`, `service`; no `router` — the
  trail is read with SQL) and an audit call on every exit path of the jira
  service. The audit write uses its **own** session: `get_session` rolls back
  on exception, so a denial recorded in the request's transaction would be
  discarded by the `NotFoundError` reporting it — allowed reads would have
  looked fine while denials were silently never recorded. Audit failure is
  fail-closed, identically for allowed and denied reads, so ADR-010 still
  holds. Added ADR-013. 49 behavioural assertions passed.

- 2026-11-08 — Step 06: the jira module and the first real endpoint.
  `GET /api/v1/issues/{issue_key}` with four ordered gates (key shape,
  identity, project access, then data) — no Jira call happens before the
  access check. Adds `app/modules/jira/` (`router`, `service`, `repository`,
  `schemas`, plus `client.py` for the Jira HTTP gateway and `adf.py` for
  Atlassian Document Format), `app/cli.py` for seeding users and grants, and
  `JIRA_ACCEPTANCE_CRITERIA_FIELD`. Added ADR-010 (a missing grant returns 404,
  not 403), ADR-011 (acceptance criteria and ADF) and ADR-012 (CLI, not admin
  endpoints). Fixed two logging leaks found while testing: `httpx` was logging
  every outbound URL at INFO, and the placeholder `JIRA_BASE_URL` resolved via
  `*.atlassian.net` wildcard DNS, so an unconfigured install sent real requests
  to Atlassian. 89 behavioural assertions passed against a mocked Jira.

- 2026-11-08 — Step 05: SQLAlchemy models and the first Alembic migration.
  `app/models/` (`users`, `user_project_access`, `jira_issues`, `jobs`,
  `audit_logs`), a constraint naming convention on `Base.metadata`, and
  `migrations/` with `env.py` on a blocking engine and no URL in
  `alembic.ini`. Revision `1dc7e5cdad8b` applied to the live Postgres 17.11;
  downgrade and re-upgrade both verified. Added ADR-008 (keys, enums,
  timestamps, constraint names) and ADR-009 (no raw Jira payload, what the
  audit trail may store). Caught a real defect: `native_enum=False` without
  `create_constraint=True` produces an unvalidated VARCHAR, so the database
  accepted `role='superuser'` — and `alembic check` does not compare CHECK
  constraints, so it reported no drift either way. 40 behavioural assertions
  passed.

- 2026-11-08 — Step 04: application entry point and health endpoints.
  `app/main.py` (wiring only: logging, request-id middleware, exception
  handlers, startup summary, pool disposal on shutdown), `app/modules/health/`
  (`/api/v1/health` liveness, `/api/v1/ready` readiness probing Postgres and
  Redis concurrently, 200 or 503) and `app/core/redis.py` (lazy client, PING
  probe, shutdown close). Added `ErrorCode.SERVICE_UNAVAILABLE` and
  `ServiceUnavailableError`, `REDIS_TIMEOUT_SECONDS`, `redis_url_safe`,
  `app.__version__`, and ADR-007. First successful `docker build`; the `api`
  container runs and reports healthy. Removed the `settings` argument from
  `create_app()`: it only reached the startup log, so an injected config was
  silently ignored by the engine, the Redis client and the router. 62
  behavioural assertions passed.

- 2026-11-08 — Step 03: `app/core`. `config.py` (one validated `Settings`,
  secrets as `SecretStr`, placeholder guard that is fatal in staging and
  production), `database.py` (lazy async SQLAlchemy engine, `get_session`,
  declarative `Base`), `logging.py` (JSON formatter, `request_id` ContextVar,
  raw-ASGI request-id middleware), `errors.py`
  (`{data, error, request_id}` envelope, `AppError` hierarchy, four exception
  handlers) and `security.py` (constant-time service-token check, then actor
  resolution). Switched to `sqlalchemy[asyncio]`, added `email-validator` and
  `DB_*` settings, and added ADR-006. 73 behavioural assertions passed against
  the live Postgres. Corrected a wrong port-binding claim in the Step 02 log.

- 2026-11-08 — Step 02: local stack. `Dockerfile` (multi-stage `api` and `mcp`
  targets, non-root, cached dependency layer), `.dockerignore`, and
  `docker-compose.yml` with Postgres and Redis (health-checked, verified
  running: PostgreSQL 17.11, Redis 7.4.12) plus the not-yet-runnable `api` and
  `mcp` services, ordered on `service_healthy`. `.env` now holds localhost
  hostnames; compose overrides the in-network ones. Added
  `MCP_TRANSPORT`/`MCP_HOST`/`MCP_PORT`, configurable `*_HOST_PORT` variables,
  ADR-004 (MCP transport) and ADR-005 (object storage deferred to Phase 3,
  because MinIO's images are no longer publicly pullable).

- 2026-11-08 — Step 01: project scaffold. Folder tree, `pyproject.toml` (Python
  3.12, uv, Ruff, pytest), `.env.example`, `.gitignore`, root `README.md`, a
  README in every empty future-phase folder, and ADR-001/002/003.

# Step 04: Application entry point and health endpoints

Date: 2026-11-08
Phase: 1

## Goal

Assemble the FastAPI application from the `app/core` pieces built in Step 3, and
expose `/health` and `/ready` under `/api/v1`. This is the first step whose
output can actually be started, so it is also the first real `docker build`.

## What was done

Created:

- `app/main.py` — the only wiring in the project. Builds the `FastAPI` object,
  installs logging, the request-id middleware and the exception handlers, mounts
  the health router under the configured prefix, logs a startup summary, and
  releases both connection pools on shutdown.
- `app/modules/health/__init__.py` — package docstring recording why this module
  has no `repository.py`.
- `app/modules/health/schemas.py` — `HealthRead`, `ReadinessRead`,
  `DependencyCheck`, `CheckStatus`. Response models, all `extra="forbid"`.
- `app/modules/health/service.py` — `build_health` and `build_readiness`. Owns
  the decision about what readiness means and which dependencies count.
- `app/modules/health/router.py` — the two endpoints. Delegates immediately.
- `app/core/redis.py` — lazy process-wide Redis client, `check_redis_connection`
  for the readiness probe, `close_redis` for shutdown. Shaped deliberately like
  `app/core/database.py` so the two read the same way.
- `docs/decisions/ADR-007-health-and-readiness-semantics.md` — why liveness and
  readiness are separate, why 503, and why `target` is environment-gated.

Changed:

- `app/__init__.py` — added `__version__`, reported by `/health`.
- `app/core/errors.py` — added `ErrorCode.SERVICE_UNAVAILABLE` and
  `ServiceUnavailableError` (503). Nothing in the Step 3 hierarchy meant
  "do not send requests yet".
- `app/core/config.py` — added `redis_timeout_seconds` and the `redis_url_safe`
  property, so logging the Redis target is safe by construction rather than by
  the author remembering to mask it.
- `.env`, `.env.example` — added `REDIS_TIMEOUT_SECONDS`.
- `docs/api.md` — documented both endpoints and the new error code.
- `README.md`, `CHANGELOG.md` — brought up to date.

## How it works

Read `app/main.py` top to bottom; it is intentionally short enough to hold in
your head.

**Startup order matters twice.**

`configure_logging` runs before the `FastAPI` object exists, so every line
printed during startup is already JSON. If it ran later, the first few lines
would be plain text and a log aggregator would reject them.

`RequestIdMiddleware` is added as the outermost middleware, so it wraps the
exception handlers rather than sitting inside them. A request that fails before
routing still gets a request id and an access-log line.

**The lifespan does not connect to anything.** It logs what the process was
configured with and then yields. A service that refuses to start because
Postgres is briefly unavailable cannot tell you *why* through `/health`, and
under an orchestrator it enters a restart loop. The engine and the Redis client
are both lazy, so the first request that needs them opens them. Shutdown calls
`dispose_engine()` and `close_redis()`; without that, a `--reload` cycle leaves
connections open until Postgres times them out.

**The two endpoints answer different questions.**

`/api/v1/health` asks "is this process alive?". It touches no dependency and
returns 200 whenever it can answer at all. Keep it that way: Kubernetes restarts
a container whose liveness probe fails, so a liveness check that depends on
Postgres turns a brief database blip into a restart storm across every pod.

`/api/v1/ready` asks "can this process serve traffic?". It probes Postgres with
`SELECT 1` and Redis with `PING`, concurrently via `asyncio.gather`, so the
endpoint costs about as long as the slowest dependency rather than the sum.
Each probe is wrapped in `_timed_probe`, which records a duration and converts
any failure into a `failed` result instead of an exception — one dead dependency
must not hide the state of the others.

If every probe passes, the router returns 200 with the check list in `data`. If
any fails, it raises `ServiceUnavailableError`, which the Step 3 handler renders
as 503 with `error.code = "service_unavailable"` and the same check list under
`error.details`. The list moves because this API guarantees that a non-2xx body
has a null `data` and a populated `error`; see ADR-007.

**The `target` field is the useful one.** It reports the host the probe actually
reached, with the password masked. Running in Docker it reads
`postgresql+psycopg://qa:***@postgres:5432/qa_platform`; running on the host it
reads `localhost:5433`. That difference is the point. In Step 3 a different
project's Postgres was holding port 5432 while ours had no host binding, and
every local connection silently went to the wrong database. This field makes
that visible in one request. It is `null` in `staging` and `production`, because
`/ready` is unauthenticated and internal hostnames are reconnaissance.

**Why `create_app()` takes no arguments.** It was written as
`create_app(settings=None)` first, which looked more testable and was not.
`get_engine()`, `get_redis()` and the router's `Depends(get_settings)` each
resolve the process-wide cached settings themselves, so an injected object
reached the startup log line and nothing else. The verification run caught it:
startup printed the injected `redis://127.0.0.1:6399/0` while the probe
connected to the real `redis://localhost:6380/0`, and the "/ready returns 503"
assertion failed because readiness was genuinely fine. Two `Settings` objects
were live inside one application. The parameter was removed rather than patched,
because an app configured one way that serves requests another way is worse than
no injection at all. To run against different configuration, patch the
environment and clear the three caches — the pattern is in the `create_app`
docstring.

**Why `health` has no `repository.py`.** The module pattern is
`router` / `service` / `schemas` / `repository`. Health owns no tables, so there
are no queries to put behind a repository; its data sources are the two probe
helpers in `app/core`. Adding an empty file to satisfy the pattern would be
worse than documenting the gap.

## How to run / verify

Note on the commands below: they are written bare, with the expected output in
prose. Interactive `zsh` does not treat `#` as a comment unless
`interactive_comments` is set, so a trailing comment is passed to the command as
an argument and produces errors like `zsh: unknown file attribute`.

Start the stack:

```
docker compose up -d api
```

Expect `postgres`, `redis` and `api` to reach `healthy`. Confirm:

```
docker compose ps
```

Three services, all `Up (healthy)`. Check that the API port was really
published, not merely requested:

```
docker inspect qa-platform-api-1 --format '{{json .NetworkSettings.Ports}}'
```

Expect `{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8000"}]}`. An empty list
means the port was not bound and every local request is reaching something else.
This is not hypothetical; see the correction in the Step 2 log.

Liveness:

```
curl -s http://127.0.0.1:8000/api/v1/health
```

Expect HTTP 200 and:

```json
{"data":{"status":"ok","service":"qa-platform","version":"0.1.0","environment":"local"},"error":null,"request_id":"931fa06a75974661a5f8260f95dfe88b"}
```

Readiness:

```
curl -s http://127.0.0.1:8000/api/v1/ready
```

Expect HTTP 200 and both checks `ok`. Inside Docker the targets are the compose
service names:

```json
{"data":{"status":"ready","checks":[{"name":"database","status":"ok","duration_ms":67.25,"target":"postgresql+psycopg://qa:***@postgres:5432/qa_platform"},{"name":"redis","status":"ok","duration_ms":1.81,"target":"redis://redis:6379/0"}]},"error":null,"request_id":"218cce405af14ab9a0a7b3bb249a4821"}
```

Prove the 503 path by stopping a dependency and putting it back:

```
docker compose stop redis
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/ready
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/health
docker compose start redis
```

Expect `503` then `200`. That ordering is the whole design: readiness drops the
instance out of the load balancer while liveness keeps the container alive.
The 503 body names the failed dependency:

```json
{"data":null,"error":{"code":"service_unavailable","message":"Service is not ready: redis unreachable.","details":{"status":"not_ready","checks":[{"name":"database","status":"ok","duration_ms":7.98,"target":"postgresql+psycopg://qa:***@postgres:5432/qa_platform"},{"name":"redis","status":"failed","duration_ms":5.86,"target":"redis://redis:6379/0"}]}},"request_id":"126a50d8bb9541398a15a17c6313ee05"}
```

Request-id handling:

```
curl -s -D - -o /dev/null -H 'X-Request-ID: my-trace-123' http://127.0.0.1:8000/api/v1/health
```

Expect `x-request-id: my-trace-123` echoed back. Send one containing a space or
a newline and expect a freshly generated hex id instead.

Logs are one JSON object per line, with the request id attached:

```
docker compose logs api --no-log-prefix
```

Lint and format:

```
uv run ruff check .
uv run ruff format --check .
```

Expect `All checks passed!` and `55 files already formatted`.

Interactive documentation is at `http://127.0.0.1:8000/docs`.

## Decisions and trade-offs

**Liveness and readiness are separate endpoints, and `/ready` returns 503.**
Recorded in ADR-007 with the alternatives. The short version: a liveness probe
that depends on Postgres causes restart storms, and orchestrators read the
status code rather than the body.

**The check list moves between `data` and `error.details`.** Keeping the
envelope invariant — non-2xx always means `data: null` plus a populated `error`
— was judged more valuable than keeping one field in one place. A client tests
`error is not None` once and is right everywhere.

**`target` is shown in `local` and `ci`, withheld in `staging` and
`production`.** A developer cannot debug a connection they cannot see, and the
Step 3 port incident is concrete evidence of the cost of hiding it. An
unauthenticated endpoint in a deployment printing internal hostnames is a
different trade. The password is masked in every environment.

**Redis is probed even though nothing uses it yet.** It is in
`docker-compose.yml` and in `Settings`, so it can already be misconfigured.
Finding that out in Phase 1 costs twenty lines; finding out in Phase 3 costs a
debugging session.

**`create_app()` takes no settings argument.** Explained above. The honest
version of the factory is less convenient than the dishonest one.

**`/docs` is enabled.** Useful internally, and every endpoint that returns real
data will require a service token. Worth revisiting if this service is ever
reachable from outside the cluster.

**`__version__` is duplicated in `app/__init__.py` and `pyproject.toml`.** The
project sets `[tool.uv] package = false`, so it is never installed and
`importlib.metadata.version` has nothing to read. Reading `pyproject.toml` at
import time would make the application depend on a file that is not needed at
runtime. Two strings and a release check is the lesser evil.

## Security notes

- **No secret reaches a response or a log.** The startup summary lists each
  field it prints explicitly rather than dumping `Settings`, and secrets are
  excluded entirely — not even a prefix, which would only shorten a brute force.
  Verified: the Jira token and the internal token appear in no log line and no
  response body.
- **Passwords are masked structurally.** `database_url_safe` and the new
  `redis_url_safe` parse the URL and replace the password with `***`. The
  readiness endpoint and every log line use those properties, never the raw
  secret. Verified that `:pw@` and a test password appear nowhere in any 503
  body.
- **Topology is withheld in deployments.** In `staging`, `/ready` returned
  `target: null` for both dependencies, and the fictional internal hostname and
  password supplied to the test appeared nowhere in the body — including on the
  503 path, which is the likelier leak, since error bodies get pasted into
  tickets.
- **Probe failures are logged, not returned.** A psycopg or redis connection
  error message can contain the host, the port and the username.
  `check_database_connection` and `check_redis_connection` log the exception and
  return `False`; the endpoint reports only `status: "failed"`.
- **Both endpoints are unauthenticated, by design.** Neither returns anything
  secret. `/health` is pure. `/ready` does do real network I/O on an
  unauthenticated path, which is a cheap amplification vector — see Known
  issues.
- **Inbound request ids are still sanitised** by the Step 3 middleware:
  alphanumerics plus `-_.`, 64 characters maximum. Confirmed against the running
  container that a value containing a space is replaced with a generated id, so
  nothing attacker-controlled is echoed into a log line or a response header.
- **No request bodies, headers or query strings are logged.** Unchanged from
  Step 3; the access line carries method, path, status and duration only.

## Known issues / TODO

- **Still no regression tests.** The 62 assertions behind this step ran from a
  scratchpad script that does not survive the session, exactly as in Step 3.
  `app/core` and now `app/main.py` and the health module have no test that would
  catch a regression. Step 8 is scoped by the brief to validation and the Jira
  service, so these need to be added deliberately. Suggested:
  `tests/unit/test_health_service.py`, `tests/integration/test_health_api.py`.
- **`/ready` is an unauthenticated endpoint that performs network I/O.** Each
  call opens or borrows a Postgres connection and a Redis connection. A loop
  against it is a cheap way to exhaust the pool. Mitigations for later: a short
  result cache, a rate limit, or binding the probe endpoints to a separate port
  not exposed outside the cluster.
- **Adding a dependency without adding a probe makes `/ready` lie.** There is no
  mechanism to catch that. The `probes` list in
  `app/modules/health/service.py` is the single place to update.
- **ADR-007 changes a Step 2 assumption.** The `api` healthcheck in
  `docker-compose.yml` targets `/api/v1/health`, which by design no longer fails
  when Postgres is down. Services declaring `depends_on: api: service_healthy`
  therefore start even when the database is unwell. That is correct for
  liveness, but anything that genuinely needs a working database must check
  `/ready` itself.
- **`__version__` can drift from `pyproject.toml`.** Nothing enforces it. A
  release check should.
- **`mcp` service still cannot start.** It needs `mcp_server/server.py` from
  Step 7, so `docker compose up` without a service name still fails on that one.
  `docker compose up -d api` is the working command until then.
- **No mypy.** Carried over from Step 3, along with the three existing
  `# type: ignore` comments.
- **Connection pool is per process.** Unchanged: `uvicorn --workers 4` at the
  default `DB_POOL_SIZE=5` and `DB_MAX_OVERFLOW=10` demands up to 60
  connections against a Postgres default `max_connections` of 100.
- **No rate limiting anywhere,** so `RateLimitedError` is still unraised.

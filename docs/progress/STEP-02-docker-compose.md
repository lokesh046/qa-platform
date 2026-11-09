# Step 02: Local stack with Docker Compose

Date: 2026-11-08
Phase: 1

## Goal

Give the project a one-command local environment: Postgres, Redis, MinIO, the
FastAPI backend, and the MCP server, with health checks and startup ordering, so
later steps can assume a working database and cache.

## What was done

### Files created

- `Dockerfile` — one multi-stage build with a shared `base` layer and two thin
  targets, `api` and `mcp`.
- `.dockerignore` — keeps `.env`, `.venv`, caches, `.git`, `docs/` and `tests/`
  out of the build context.
- `docker-compose.yml` — four services: `postgres`, `redis`, `api`, `mcp`. Two
  named volumes, one bridge network. Object storage is **not** included; see
  ADR-005.
- `docs/decisions/ADR-004-mcp-transport.md` — why the MCP server speaks both
  stdio and HTTP.
- `docs/decisions/ADR-005-object-storage-deferred.md` — why there is no MinIO.

### Files changed

- `.env.example` — hostnames switched to the localhost variants (see below);
  `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` added; configurable host ports
  (`POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `API_HOST_PORT`, `MCP_HOST_PORT`)
  added; the `MINIO_*` block replaced by a commented-out vendor-neutral `S3_*`
  block. A command for generating `INTERNAL_API_TOKEN` was added as a comment.
- `.env` — created locally from the template. Gitignored, placeholders only.
  `REDIS_HOST_PORT` is set to `6380` on this machine, because a Homebrew
  `redis-server` already owns 6379.
- `README.md` — "Running locally" rewritten with real commands,
  "Project structure" updated, and the environment-variable table extended.
- `CHANGELOG.md` — Step 02 line.

## How it works

### One Dockerfile, two images

Both processes run the same code and the same dependencies, so they share a
`base` stage. The order of instructions in that stage is deliberate:

1. Pin `uv==0.12.10` — the same version as the host, so a container rebuild
   resolves what the local checkout resolved.
2. Copy **only** `pyproject.toml` and `uv.lock`, then `uv sync --frozen --no-dev`.
   Editing a `.py` file does not invalidate this layer, so rebuilds take seconds
   instead of a minute. `--frozen` makes the build fail if `uv.lock` disagrees
   with `pyproject.toml`, rather than silently resolving something else.
3. Create an unprivileged user (uid 10001) and copy the code owned by it.
4. `USER appuser`. Neither process needs root.

The dependency venv lives at `/opt/venv`, **not** inside `/srv/app`. That matters
because local development bind-mounts `./app` over `/srv/app/app`; if the venv
were inside the workdir, the mount would hide it and every import would fail.

`PYTHONPATH=/srv/app` is set because `pyproject.toml` has
`[tool.uv] package = false` — the project is never installed as a distribution,
so its import root has to be stated explicitly (ADR-001).

The `api` and `mcp` stages add only `EXPOSE`, a health check, and a `CMD`.

### Startup ordering

`depends_on` with `condition: service_healthy` is the whole trick. Compose's
plain `depends_on` only waits for a container to *start*, which for Postgres
means "the process exists", not "the database accepts connections" — initdb
takes several seconds on first run. With a health check and
`service_healthy`, the API container is not created until `pg_isready`
succeeds, so the first thing the API does on boot cannot be a connection
refusal.

The chain is:

```
postgres ─┐
          ├─► api ─► mcp
redis    ─┘
```

Each health check probes the real protocol, not the process: `pg_isready` for
Postgres, `redis-cli ping` for Redis, and an HTTP request to `/api/v1/health`
for the API. The MCP container's check is a TCP connect, because the MCP HTTP
endpoint expects a session-initialising POST and would reject a bare probe
(ADR-004).

### The localhost / service-name split

There are two ways to run the API: inside compose, where Postgres answers on the
hostname `postgres`, and from your shell with `uv run`, where it answers on
`localhost`. Keeping two env files for that gets out of sync within a week.

Instead, `.env` holds the **localhost** values, and `docker-compose.yml`
overrides the four affected variables in the `environment:` block of the service
that needs them:

| Variable | `.env` (your shell) | compose override |
| --- | --- | --- |
| `DATABASE_URL` | `...@localhost:5432/...` | `...@postgres:5432/...` |
| `REDIS_URL` | `redis://localhost:6379/0` | `redis://redis:6379/0` |
| `BACKEND_BASE_URL` | `http://localhost:8000` | `http://api:8000` (on `mcp`) |

An `environment:` entry always wins over `env_file:`, so this needs no
conditional logic anywhere.

### Correction, found during Step 3

Step 2 originally recorded that `docker compose ps` showing the Postgres
mapping as a bare `5432/tcp` was "a Compose v5.5.1 display quirk, not a wider
bind". **That was wrong.** The port genuinely was not published.

An unrelated container on this machine, `hearth-local-pg` (postgres:16-alpine,
another project), already owned `127.0.0.1:5432`. Our container started without
a host mapping, and `docker compose up` did not treat that as an error. The
misleading part is that `nc -z 127.0.0.1 5432` still succeeded and a client
still connected — to the *other project's database*, which is what produced
`FATAL: password authentication failed for user "qa"` during Step 3.

The lesson is in the commands. `HostConfig.PortBindings` shows what was
*requested*; `NetworkSettings.Ports` shows what was *actually bound*:

```bash
docker inspect qa-platform-postgres-1 --format '{{json .NetworkSettings.Ports}}'
```

Before the fix that printed `{"5432/tcp":[]}`; after moving to
`POSTGRES_HOST_PORT=5433` it prints
`{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5433"}]}`.

Checking a port with `nc` proves only that *something* is listening. Always
confirm the mapping belongs to the container you meant.

### Host ports are configurable

The published host port of every service comes from an environment variable
with the conventional default: `${REDIS_HOST_PORT:-6379}` and so on. This turned
out to matter on the first run — a Homebrew `redis-server` already held 6379,
and `docker compose up` failed with
`bind: address already in use`. Rather than stopping a service the developer may
be using, `.env` sets `REDIS_HOST_PORT=6380` and `REDIS_URL` to match. The
container still listens on 6379 internally, so nothing inside the compose
network changes.

### Required variables fail loudly

Database and MinIO credentials use the `${VAR:?message}` form. If the variable
is unset, Compose refuses to start and prints the message:

```
required variable POSTGRES_PASSWORD is missing a value: set POSTGRES_PASSWORD in .env
```

That is better than a Postgres container that starts with a blank password or an
API that reaches a database it was not meant to reach.

### Ports are loopback-only

Every published port is written `127.0.0.1:<port>:<port>` rather than
`<port>:<port>`. The bare form binds `0.0.0.0`, which on a laptop on a café
network publishes your database to that network — Docker's port publishing
bypasses the macOS firewall. Loopback binding keeps the ports reachable from
your shell and from nothing else.

## How to run / verify

All of the following was run against a live Docker daemon (29.7.2,
linux/aarch64) on 2026-11-08 and passed.

Code blocks contain bare commands only. Zsh does not treat `#` as a comment
interactively unless you run `setopt interactive_comments`, so an annotated
command pasted into a terminal fails in confusing ways
(`zsh: unknown file attribute`). Expected output is given as prose instead.

### 1. Compose file is valid and resolves

```bash
docker compose config --quiet
docker compose config --services
```

Exits 0, and lists `redis`, `postgres`, `api`, `mcp`.

### 2. In-network overrides win over .env

```bash
docker compose config | grep -E 'DATABASE_URL|REDIS_URL'
docker compose config | grep -E 'MCP_TRANSPORT|BACKEND_BASE_URL'
```

Observed `@postgres:5432`, `redis://redis:6379/0`, `MCP_TRANSPORT: http`, and
`BACKEND_BASE_URL: http://api:8000` on the `mcp` service.

### 3. A missing credential stops the stack

```bash
mv .env .env.bak
docker compose config --quiet
mv .env.bak .env
```

The middle command exits 1 and prints
`required variable POSTGRES_PASSWORD is missing a value: set POSTGRES_PASSWORD in .env`.

### 4. Infrastructure starts and becomes healthy

```bash
docker compose up -d postgres redis
docker compose ps
```

Both services reach `healthy` within about four seconds on a warm image cache.

### 5. Postgres is actually usable, with the intended encoding

```bash
docker compose exec postgres psql -U qa -d qa_platform -c 'select version();'
docker compose exec postgres psql -U qa -d qa_platform -c "select current_database(), pg_encoding_to_char(encoding), datcollate from pg_database where datname='qa_platform';"
```

Observed `PostgreSQL 17.11 on aarch64-unknown-linux-musl`, and
`qa_platform | UTF8 | C` — which confirms `POSTGRES_INITDB_ARGS` took effect.

### 6. Redis is usable and persistence is configured

```bash
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli config get appendonly
```

Observed `PONG` and `appendonly yes`. Redis reports v7.4.12.

### 7. The compose Redis is reachable from the host, and is a separate instance

```bash
redis-cli -h 127.0.0.1 -p 6380 dbsize
redis-cli -h 127.0.0.1 -p 6379 dbsize
```

Observed `0` on 6380 (the compose container) and `4` on 6379 (the pre-existing
Homebrew instance). Two separate Redises; the project does not touch the local
one.

### 8. Lint is still clean

```bash
uv run ruff check .
uv run ruff format --check .
```

### 9. Tear down

```bash
docker compose down
```

Add `-v` to delete the `pgdata` and `redisdata` volumes as well.

### What is not verified

**No image has been built.** `docker build` was not run, so the `Dockerfile` is
validated by reading only. The `api` service cannot start regardless, because
`uvicorn app.main:app` needs `app/main.py` from Step 4, and `mcp` waits on
`api` being healthy and needs `mcp_server/server.py` from Step 7. The first
honest end-to-end `docker compose up` is at the end of Step 7, which is also
when the project's "`docker compose up` starts all services" criterion can
first be met.

## Decisions and trade-offs

Two decisions needed their own records:

- **ADR-004** — the MCP server supports both stdio and HTTP, because a stdio
  process cannot be a meaningful compose service while Copilot Chat requires
  stdio.
- **ADR-005** — there is no object storage in the stack. Every MinIO image is
  now behind registry authentication, and nothing reads object storage before
  Phase 3, so the provider choice moves to the step that writes the first
  artifact.

Smaller choices:

- **One multi-stage Dockerfile rather than two files.** The two images differ by
  three lines. Two Dockerfiles would duplicate the dependency layer and halve
  the build-cache hit rate.
- **`pip install uv` rather than copying from the `ghcr.io/astral-sh/uv` image.**
  Copying the binary is faster, but it means pinning a second image tag. A
  pinned PyPI version is one less moving part and is verifiable from the host.
- **Bind-mount the source and run `--reload` for the API.** Editing a file
  restarts the app in about a second, with no rebuild. The mounts are `:ro`, so
  a bug in the container cannot write to your checkout. Cost: the compose
  `command` diverges from the image `CMD`, so the image's production behaviour
  is only exercised in CI. Worth it for the iteration speed.
- **`redis --appendonly yes --save ""`.** The append-only log survives a restart
  and RDB snapshots are off, which avoids two overlapping persistence
  mechanisms. Phase 4 puts a job queue here, where losing a restart's worth of
  work would matter.
- **`POSTGRES_INITDB_ARGS="--encoding=UTF8 --locale=C"`.** Fixes collation, so
  `ORDER BY` on text behaves identically on a developer machine and in CI. It
  has to be set at initdb time; changing it later means recreating the volume.
- **Host ports come from environment variables with conventional defaults.**
  Cost: four more variables. Benefit: a port collision is a one-line `.env`
  change instead of a decision about stopping someone's local database. This
  paid for itself on the first run.
- **`tests/` excluded from the image.** Smaller runtime image and less in it to
  go wrong. Cost: you cannot `docker compose exec api pytest`. CI runs
  `uv run pytest` on the host instead.

## Security notes

- `.dockerignore` lists `.env` first. Without it, `COPY . .` would bake real
  credentials into an image layer, where they survive even if a later layer
  deletes the file.
- Both images run as uid 10001, not root. A container escape through the web
  process starts from an unprivileged account.
- Every published port binds `127.0.0.1`. Nothing in this stack is reachable
  from the local network.
- Credentials reach containers through `env_file`/`environment` only. No
  credential appears in `docker-compose.yml`, the `Dockerfile`, or any committed
  file. `.env.example` holds placeholders.
- The `${VAR:?...}` guards mean a missing credential stops the stack instead of
  starting Postgres with a blank password.
- No object-storage credentials exist anywhere now, which removes a whole class
  of exposure from Phase 1. Phase 3 should give the application a scoped service
  account rather than a root key.
- `docker compose config` prints secrets in full. Do not paste its raw output
  into a ticket or a chat; the verification commands above filter or mask it.

## Known issues / TODO

- **Images are pinned to a major line, not a digest.** `postgres:17-alpine`
  resolved to 17.11 and `redis:7-alpine` to 7.4.12 on this machine. The digests
  observed were:

  ```
  postgres@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac
  redis@sha256:c5d34a98396a4775e223c74e929162c35c5a2182901ba26b50ade939941ce14b
  ```

  A major-line tag keeps receiving security patches, which is usually what you
  want for infrastructure; pin by digest if CI reproducibility matters more.
- **Full `docker compose up` fails until Step 7.** `api` needs `app/main.py`
  (Step 4) and `mcp` needs `mcp_server/server.py` (Step 7). Until then use
  `docker compose up -d postgres redis`.
- **No image has been built.** The Dockerfile is validated by reading, not by
  `docker build`. In particular `uv sync --frozen --no-dev` in a container, the
  `/opt/venv` path, and the `PYTHONPATH` arrangement are untested. Expect to fix
  something on the first real build, in Step 4.
- **Object storage is absent** (ADR-005). Phase 3 must choose a provider, add
  the service, create the bucket, and name the `S3_*` variables.
- **No migration runner.** Step 5 must decide whether Alembic runs as an
  init container, an entrypoint step, or a manual command. An entrypoint
  migration is convenient and races when two API replicas start together.
- **No resource limits.** Fine on a laptop; a shared environment would want
  `deploy.resources`.
- **`REDIS_HOST_PORT=6380` and `POSTGRES_HOST_PORT=5433` are specific to this
  machine**, where a Homebrew Redis and another project's Postgres container
  hold the conventional ports. `.env.example` ships 6379 and 5432.
- **A port collision can be silent.** Compose refused to start Redis but
  started Postgres with no mapping at all. Worth a startup check: Step 4's
  `/ready` endpoint should report the database it actually reached, so this
  failure mode announces itself.
- **The MCP HTTP listener has no caller authentication** (ADR-004). Acceptable
  only while it binds loopback.
- **No CI workflow** builds these images yet.

## 3-line summary

- **What changed:** added `Dockerfile` (multi-stage, `api` and `mcp` targets,
  non-root, cached dependency layer), `.dockerignore`, and
  `docker-compose.yml` (Postgres and Redis with protocol-level health checks,
  `service_healthy` ordering, loopback-only configurable host ports, `${VAR:?}`
  guards, plus the not-yet-runnable `api` and `mcp` services); moved `.env` to
  localhost hostnames with compose overriding the in-network ones; added
  `MCP_TRANSPORT`/`MCP_HOST`/`MCP_PORT` and the four `*_HOST_PORT` variables;
  dropped object storage after MinIO's images turned out to be gated.
- **Where it is documented:** `docs/progress/STEP-02-docker-compose.md`,
  `docs/decisions/ADR-004-mcp-transport.md`,
  `docs/decisions/ADR-005-object-storage-deferred.md`, `README.md`,
  `CHANGELOG.md`.
- **Next step:** Step 3 — `app/core`: settings from the environment, the DB
  session, JSON logging with `request_id`, and the standard
  `{data, error, request_id}` error response.

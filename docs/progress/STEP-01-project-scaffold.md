# Step 01: Project scaffold

Date: 2026-11-08
Phase: 1

## Goal

Create the complete folder tree, dependency manifest, environment-variable
template, and documentation skeleton, so every later step has a known place to
put its code. No application code yet.

## What was done

### Root files

- `pyproject.toml` — dependencies, Python pin (`>=3.12,<3.13`), and the Ruff and
  pytest configuration.
- `uv.lock` — the resolved dependency set, created by `uv sync`. Committed so
  CI and the containers in Step 2 resolve identical versions.
- `.env.example` — every environment variable Phase 1 will read, with
  placeholder values only.
- `.gitignore` — keeps `.env`, `.venv`, caches, and local data volumes out of Git.
- `README.md` — Setup, Running locally, Project structure, Environment variables.
- `CHANGELOG.md` — one dated line per step, newest first.

### Package tree

- `app/` with `core/`, `models/`, `modules/`, `llm/prompts/`, `workflows/`.
- `app/modules/` with the four Phase 1 modules (`users`, `jira`, `jobs`,
  `audit`) and the seven later-phase placeholders.
- `mcp_server/` — the MCP gatekeeper package.
- `workers/sandbox/`, `specs/openspec/{specs,changes}/`,
  `templates/playwright/`, `migrations/`, `tests/{unit,integration}/`, `docs/`,
  `.vscode/`.
- An `__init__.py` in every Python package, so imports work from the repository
  root before any module code exists.

### Documentation

- `app/modules/README.md` — the four-file module pattern and the direction of
  calls.
- A `README.md` in each empty future-phase folder stating its purpose, the phase
  that fills it, and what must not go there.
- `docs/decisions/ADR-001-repository-layout-and-tooling.md`
- `docs/decisions/ADR-002-jira-cloud-service-account-auth.md`
- `docs/decisions/ADR-003-mcp-to-backend-auth.md`
- `docs/api.md` and `docs/mcp-tools.md` — contract files, empty of entries until
  Step 4 and Step 7 add them.
- `docs/progress/STEP-01-project-scaffold.md` — this file.

### Deliberately not created

`app/core/config.py`, `app/main.py`, `mcp_server/server.py` and their siblings
are named in the README but not created as empty files. An empty `.py` file that
imports cleanly is worse than an absent one: the lint passes, the module looks
done, and nothing tells you it is a stub. Each file appears in the step that
implements it.

## How it works

Three ideas hold the layout together.

**The MCP server is a gatekeeper, not a service.** `mcp_server/` authenticates
the caller, validates arguments, writes an audit record, and forwards the call
over HTTP. It is deliberately forbidden from importing `app`. If you ever feel
the need to add an `if` statement about *business* meaning in `mcp_server/`, it
belongs in a backend service instead.

**Every backend capability is a module with the same four files.** `router.py`
holds FastAPI endpoints that parse input, call the service, and return.
`service.py` holds the logic and knows nothing about HTTP. `schemas.py` holds
the Pydantic request and response models. `repository.py` holds the database
queries and is the only place SQLAlchemy appears. Calls go router to service to
repository, never backwards. The payoff is that you can find anything by
guessing, and you can unit-test a service without starting a web server.

**Cross-cutting concerns live in `app/core/` and nowhere else.** Settings,
database sessions, logging, the error envelope, and the service-token check are
each one file that everything else imports. There is no second place where a
config value is read from `os.environ`.

Dependencies are managed by uv against a single `pyproject.toml`. Because
`[tool.uv] package = false`, uv installs the dependencies but does not build
this project as a wheel — it is an application, not a library. The practical
consequence is that you run things from the repository root so that `app` and
`mcp_server` are importable.

Ruff enforces the brief's rules mechanically rather than by review: `ANN`
requires type annotations, `D` requires docstrings on public classes and
functions, and `S` runs the bandit security checks. Tests are exempted from the
assert and docstring rules through `per-file-ignores`.

## How to run / verify

Code blocks contain bare commands only. Zsh does not treat `#` as a comment
interactively unless you run `setopt interactive_comments`, so pasting an
annotated command into a terminal fails in confusing ways. Expected output is
given as prose.

```bash
cd /Users/yeswanth/Documents/jira_mcp
uv sync
```

Installs dependencies and creates `.venv/`. uv fetches Python 3.12 if the host
lacks it.

```bash
uv run python -V
```

Observed `Python 3.12.14`, not the host's 3.14.

```bash
uv run ruff check .
uv run ruff format --check .
```

Observed `All checks passed!` and `39 files already formatted`.

```bash
uv run python -c "import app, mcp_server; print('packages import')"
```

Observed `packages import`.

```bash
uv run pytest
```

Observed `no tests ran in 0.00s`, exit code 0. Tests arrive in Step 8.

```bash
find . -name README.md -not -path './.venv/*' -not -path './.pytest_cache/*' | sort
```

Observed 17 files: the root README, `app/modules/README.md`, and 15
placeholder-folder READMEs.

```bash
cp .env.example .env
```

Then fill in `JIRA_*` and `INTERNAL_API_TOKEN`.

## Decisions and trade-offs

Three decisions were significant enough for their own records:

- **ADR-001** — repository layout, uv as the dependency manager, the four-file
  module pattern, and Ruff as the single lint/format tool.
- **ADR-002** — Jira Cloud only, read with one read-only service account, with
  access control enforced by our own `user_project_access` table.
- **ADR-003** — the MCP server authenticates to the backend with a shared
  service token plus an `X-Actor-Email` header.

Smaller choices made here:

- **`__init__.py` everywhere rather than namespace packages.** Explicit packages
  make the import roots obvious and keep pytest's collection predictable.
- **Stdlib logging with a JSON formatter, not structlog.** Step 3 needs JSON
  lines with a `request_id`; that is about thirty lines of stdlib code and one
  fewer dependency. Revisit if log enrichment spreads.
- **`asyncio_default_fixture_loop_scope = "function"` is set explicitly.**
  pytest-asyncio warns when it is unset and will change the default in a future
  release; pinning it now means the test suite behaves the same after an upgrade.
- **No mypy yet.** Ruff's `ANN` rules force annotations to exist but do not
  check that they are right. Adding mypy is worth doing once there are models
  and services to check, around Step 5.
- **Phase 1 module folders get no placeholder README**, unlike the later-phase
  folders, because `app/modules/README.md` already describes what goes in them
  and they stop being empty within a few steps.

## Security notes

- `.env` is in `.gitignore`; `.env.example` is committed and contains
  placeholders only. No token, password, or tenant URL in this commit is real.
- Secret-bearing variables are marked **Secret** in the README table:
  `POSTGRES_PASSWORD`, `DATABASE_URL`, `MINIO_ROOT_PASSWORD`, `JIRA_API_TOKEN`,
  `INTERNAL_API_TOKEN`.
- Ruff's `S` rules are on, so hardcoded passwords, `assert` in production code,
  and unsafe subprocess or `yaml.load` calls fail the lint from now on.
- `MINIO_ROOT_PASSWORD` has a minimum length of 8 characters; MinIO refuses to
  start below that, which is worth knowing before Step 2.
- `workers/sandbox/README.md` records the rule that generated Playwright code
  only ever runs in the sandboxed worker, never in the API process, so that
  constraint is written down before anyone can be tempted.

## Known issues / TODO

- `docker-compose.yml` does not exist yet (Step 2), so `docker compose up` will
  fail. The README's "Running locally" section says as much.
- `migrations/` holds only a README. Step 5 runs `alembic init migrations`,
  which will add `env.py`, `script.py.mako`, and `alembic.ini`.
- No Git repository has been initialised. Worth doing before Step 2 so the
  `.gitignore` is in effect from the first commit.
- `docs/api.md` and `docs/mcp-tools.md` exist with headers and no entries. They
  stay that way until Step 4 and Step 7.
- No CI workflow. The lint, format, and test commands above are the contract a
  future GitHub Actions workflow should run.

## 3-line summary

- **What changed:** created the full folder tree with `__init__.py` files,
  `pyproject.toml` (Python 3.12, FastAPI/SQLAlchemy/MCP dependencies, Ruff and
  pytest config), `.env.example`, `.gitignore`, the root `README.md`, and a
  README in every empty future-phase folder.
- **Where it is documented:** `docs/progress/STEP-01-project-scaffold.md`,
  ADR-001/002/003 in `docs/decisions/`, `CHANGELOG.md`, and `README.md`.
- **Next step:** Step 2 — `docker-compose.yml` with Postgres, Redis, MinIO, the
  API, and the MCP server.

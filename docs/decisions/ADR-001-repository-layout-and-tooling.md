# ADR-001: Repository layout and dependency management

Date: 2026-11-08
Status: Accepted

## Context

Phase 1 needs a layout that two separate processes share: a FastAPI backend and
an MCP server. Later phases add workers, workflow definitions, Playwright
templates, and OpenSpec artifacts. We also had to choose how dependencies are
declared and installed, and where the tree lives on disk.

The working directory `/Users/yeswanth/Documents/jira_mcp` was empty, while the
project brief named the root `qa-platform/`.

## Decision

1. **The repository root is the project root.** Everything lives directly in
   `jira_mcp/` rather than in a nested `qa-platform/` folder. Confirmed with the
   project owner. The distribution name in `pyproject.toml` is still
   `qa-platform`.
2. **One repository, two applications.** `app/` is the backend, `mcp_server/`
   is the gatekeeper. They share the repository and the Python version but not
   code: `mcp_server` must not import from `app`.
3. **uv for dependencies**, with a single `pyproject.toml` and
   `[tool.uv] package = false`. The project is an application, so there is no
   build backend and no wheel to build; `uv sync` installs dependencies into
   `.venv` and the packages import from the repository root.
4. **Python pinned to `>=3.12,<3.13`.** The host machine has 3.14; uv fetches
   3.12 for the virtual environment so local and container runtimes match.
5. **Four files per module** — `router.py`, `service.py`, `schemas.py`,
   `repository.py` — with calls flowing router to service to repository and
   never back.
6. **Ruff for both linting and formatting**, with annotation (`ANN`),
   docstring (`D`), and security (`S`) rules enabled so the brief's "type hints
   everywhere" and docstring requirements are machine-checked rather than
   remembered.
7. **Every empty future-phase folder carries a README** naming its purpose and
   the phase that fills it, so the tree is self-explanatory before the code
   exists.

## Alternatives

- **A src/ layout with a real build backend (hatchling).** Correct for a
  library, unnecessary here: nothing installs this project as a dependency, and
  it adds a build step to every `uv sync`.
- **Two repositories, one per process.** Cleaner deployment boundary, but in
  Phase 1 the two move together on nearly every change, and a shared
  `.env.example` and compose file are easier to keep honest in one repository.
- **Poetry or pip-tools.** Both work. uv was specified in the brief, is
  considerably faster, and manages the Python toolchain itself, which removes
  the "which 3.12 is on PATH" problem.
- **mypy in addition to Ruff.** Deferred. Ruff's `ANN` rules force annotations
  to exist; they do not check that the annotations are correct. Worth adding
  once the models and services exist, probably alongside Step 5.
- **A monorepo package per module.** Over-engineering for four Phase 1 modules.

## Consequences

- `uv sync` plus `cp .env.example .env` is the whole setup. No build step.
- Because `package = false`, anything run from the repository must go through
  `uv run` (or an activated `.venv`) from the repository root, so that `app` and
  `mcp_server` are importable. Containers set `WORKDIR` accordingly in Step 2.
- Ruff's `D` and `ANN` rules will fail the lint on any new function without a
  docstring or annotations. That is the intent, but it means every later step
  must write docstrings as it goes rather than afterwards.
- `mcp_server` not importing `app` means the two duplicate a small amount of
  shape knowledge (the HTTP contract). That duplication is deliberate: it is
  what keeps the MCP server a thin client rather than a second copy of the
  backend.
- Adding a fifth file to a module (say `tasks.py`) is a change to the module
  pattern and should be recorded as a new ADR.

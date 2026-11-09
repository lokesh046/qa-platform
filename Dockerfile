# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Shared base: Python 3.12 plus the locked dependency set.
#
# Dependencies are installed before the application code is copied, so editing
# a .py file does not invalidate the dependency layer. uv itself is pinned so
# a rebuild resolves exactly what the local checkout resolved.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Install into a venv outside the workdir, so a bind mount of the source
    # during local development cannot shadow it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    # This project sets [tool.uv] package = false, so it is never installed as a
    # distribution. The code is importable because its root is on PYTHONPATH.
    PYTHONPATH=/srv/app

RUN pip install --no-cache-dir "uv==0.12.10"

WORKDIR /srv/app

# Dependency layer. --frozen fails the build if uv.lock disagrees with
# pyproject.toml, rather than quietly resolving something different.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Unprivileged user. Nothing in either process needs root.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser . .

USER appuser


# ---------------------------------------------------------------------------
# api: the FastAPI backend.
# The compose file overrides this command with --reload for local development.
# ---------------------------------------------------------------------------
FROM base AS api

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ---------------------------------------------------------------------------
# mcp: the MCP gatekeeper.
#
# Copilot Chat launches this over stdio on a developer machine. Inside compose
# there is no stdio peer, so MCP_TRANSPORT=http makes it listen on a port
# instead. See docs/decisions/ADR-004-mcp-transport.md.
# ---------------------------------------------------------------------------
FROM base AS mcp

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import socket, sys; s = socket.create_connection(('127.0.0.1', 8765), timeout=3); s.close(); sys.exit(0)"]

CMD ["python", "-m", "mcp_server.server"]

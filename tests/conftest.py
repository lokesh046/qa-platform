"""Shared fixtures.

Two rules hold for everything under ``tests/``:

**No infrastructure.** Nothing here opens a database connection, a Redis
connection or a socket. The jira service is tested with its repository stubbed,
Jira is mocked with ``respx``, and the MCP tool runs over the SDK's in-process
transport. ``uv run pytest`` therefore passes on a clean checkout with nothing
running, which is the whole reason the Step 1-7 verification harnesses could
never be committed: they needed the dev database and seeded rows into it.

**No ambient configuration.** :func:`_hermetic_environment` is autouse and
removes every variable this project reads from ``os.environ``, and every
``Settings`` object is built with ``_env_file=None``. Without both, a test
would quietly inherit whatever is in the developer's ``.env`` — and would then
pass or fail depending on which machine ran it.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.config import Settings
from mcp_server.config import McpSettings

#: A token long enough to satisfy both settings classes' minimum length.
#: Fake, obviously, and 32+ characters so the length check passes.
TEST_TOKEN = "unit-test-internal-token-0123456789abcdef"

#: Jira's host in these tests. ``respx`` intercepts it, so no DNS lookup or
#: TCP connection happens, but it must still be a valid https URL.
TEST_JIRA_BASE_URL = "https://acme-qa.atlassian.net"

#: The backend's address as the MCP server sees it in these tests.
TEST_BACKEND_BASE_URL = "http://backend.acme-qa.co"

#: The end user every test acts as. ``.test``/``.invalid`` are rejected by
#: ``email-validator``, so test addresses use a normal-looking domain.
TEST_ACTOR = "qa.one@acme-qa.co"

#: Every environment variable either settings class reads. Removed before each
#: test so the suite cannot inherit the developer's shell or ``.env``.
_PROJECT_ENV_VARS = (
    "APP_ENV",
    "APP_NAME",
    "API_V1_PREFIX",
    "LOG_LEVEL",
    "DATABASE_URL",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT_SECONDS",
    "DB_ECHO",
    "REDIS_URL",
    "REDIS_TIMEOUT_SECONDS",
    "JIRA_BASE_URL",
    "JIRA_SERVICE_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_TIMEOUT_SECONDS",
    "JIRA_CACHE_TTL_SECONDS",
    "JIRA_ACCEPTANCE_CRITERIA_FIELD",
    "INTERNAL_API_TOKEN",
    "MCP_TRANSPORT",
    "MCP_HOST",
    "MCP_PORT",
    "BACKEND_BASE_URL",
    "BACKEND_TIMEOUT_SECONDS",
    "QA_ACTOR_EMAIL",
)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove this project's environment variables for the duration of a test.

    Autouse, and deliberately not optional. A test that needs a value sets it
    itself with ``monkeypatch.setenv``, which makes the dependency visible in
    the test rather than in whoever's shell happens to run it.
    """
    for name in _PROJECT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: Any) -> Settings:
    """Build backend :class:`Settings` with valid defaults and no ``.env``.

    ``_env_file=None`` is the important argument. ``Settings`` declares
    ``env_file=".env"``, and the tests run from the repository root where that
    file exists, so without this a test would read the developer's real
    configuration — including their real Jira host.
    """
    values: dict[str, Any] = {
        "app_env": "local",
        "database_url": "postgresql+psycopg://qa:pw@localhost:5432/qa_platform",
        "jira_base_url": TEST_JIRA_BASE_URL,
        "jira_service_email": "svc@acme-qa.co",
        "jira_api_token": "jira-api-token-for-tests",
        "internal_api_token": TEST_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_mcp_settings(**overrides: Any) -> McpSettings:
    """Build :class:`McpSettings` with valid defaults and no ``.env``."""
    values: dict[str, Any] = {
        "internal_api_token": TEST_TOKEN,
        "backend_base_url": TEST_BACKEND_BASE_URL,
        "mcp_transport": "stdio",
        "qa_actor_email": TEST_ACTOR,
    }
    values.update(overrides)
    return McpSettings(_env_file=None, **values)


@pytest.fixture
def settings() -> Settings:
    """Backend settings with the acceptance-criteria custom field unset."""
    return make_settings()


@pytest.fixture
def mcp_settings() -> McpSettings:
    """MCP settings on the stdio transport, acting as :data:`TEST_ACTOR`."""
    return make_mcp_settings()


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    """Capture log output for the secret-hygiene assertions.

    Read it with ``log_buffer.getvalue()`` after the code under test has run.

    ``caplog`` would be the idiomatic choice, but it records the unformatted
    message and the ``extra`` dict separately. What matters for a secret is the
    *rendered* output, because that is what reaches a file or a log aggregator,
    so this attaches a real handler with a real formatter.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

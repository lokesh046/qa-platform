"""Configuration for the MCP gatekeeper.

A settings class of its own, deliberately **not** ``app.core.config.Settings``.

Reusing the backend's settings would be less code and would have been the
obvious thing to do. It would also mean this process requires — and can read —
``DATABASE_URL`` and ``JIRA_API_TOKEN``. The whole argument for running the
gatekeeper as a separate process is that compromising it should not reach the
data. A settings class that demands the database password throws that away on
the first line of the file.

So this declares only what the gatekeeper genuinely needs: where the backend
is, the token to call it with, how long to wait, and who the caller is.

Two caveats, because the separation is real but not absolute:

- In the container it is enforced. The ``mcp`` service in
  ``docker-compose.yml`` deliberately has no ``env_file``; it lists its seven
  variables one at a time, so ``DATABASE_URL`` and ``JIRA_API_TOKEN`` are
  never in the process environment at all.
- On the host, running over stdio, this reads the same ``.env`` as the backend
  because a developer should not maintain two files. The variables are
  therefore present in the environment, and ``app.core.config`` is importable.
  What this class gives you is that nothing in ``mcp_server`` asks for them,
  so using one would be a visible change rather than an accident.
"""

from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

McpTransport = Literal["stdio", "http"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: Matches app.core.config. A token shorter than this is almost certainly a
#: placeholder that someone forgot to replace.
_MIN_TOKEN_LENGTH = 32


class McpSettings(BaseSettings):
    """Validated configuration for the MCP server process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        str_strip_whitespace=True,
    )

    log_level: LogLevel = "INFO"

    # --- Transport ---------------------------------------------------------
    # stdio for Copilot Chat, which launches the process itself; http for the
    # container. See ADR-004 and ADR-014.
    mcp_transport: McpTransport = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65535)

    # --- Backend -----------------------------------------------------------
    backend_base_url: str = "http://localhost:8000"
    backend_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    internal_api_token: SecretStr

    # --- Caller identity ---------------------------------------------------
    # Used only on stdio, where the process is launched by one developer and
    # the process boundary *is* the identity. On http this is ignored and a
    # per-request header is required instead; see ADR-014.
    qa_actor_email: EmailStr | None = None

    @field_validator("backend_base_url")
    @classmethod
    def _check_backend_url(cls, value: str) -> str:
        """Require an absolute http(s) URL and strip any trailing slash."""
        candidate = value.strip().rstrip("/")
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("BACKEND_BASE_URL must start with http:// or https://")
        if "://" in candidate and not candidate.split("://", 1)[1]:
            raise ValueError("BACKEND_BASE_URL has no host")
        return candidate

    @field_validator("internal_api_token")
    @classmethod
    def _check_token(cls, value: SecretStr) -> SecretStr:
        """Reject an obviously unset token.

        Reports the length only. Echoing any part of a token into an error
        message puts it in whatever captured that message.
        """
        raw = value.get_secret_value()
        if len(raw) < _MIN_TOKEN_LENGTH:
            raise ValueError(
                f"INTERNAL_API_TOKEN is {len(raw)} characters; at least "
                f"{_MIN_TOKEN_LENGTH} are required. Generate one with "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return value

    @property
    def is_http(self) -> bool:
        """True when serving over HTTP rather than stdio."""
        return self.mcp_transport == "http"


def load_settings() -> McpSettings:
    """Build the settings, reading the environment.

    Not cached. The process builds them once at startup and passes the object
    down, so a cache would only hide where the values come from.
    """
    return McpSettings()  # type: ignore[call-arg]  # values come from the environment

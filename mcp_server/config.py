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

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

McpTransport = Literal["stdio", "http"]
McpAuthMode = Literal["oauth", "service_token"]
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

    # --- Remote access and identity (http only; see ADR-022) ---------------
    # oauth: this server is an OAuth 2.1 authorization server, GitHub performs
    #   the login, and the access token carries the user's verified email.
    # service_token: the pre-ADR-022 behaviour, where a shared token plus an
    #   X-Actor-Email header is accepted. Retained so the local compose stack
    #   works before a GitHub OAuth app exists. It trusts the header, so it is
    #   not a way to expose the port.
    mcp_auth_mode: McpAuthMode = "oauth"

    #: The externally reachable base URL of this server. Becomes the OAuth
    #: issuer, so it must be what the client actually dialled: a client that
    #: reached a tunnel and is handed a localhost issuer will refuse.
    mcp_public_url: str = "http://127.0.0.1:8765"

    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None

    #: Signs our own access and refresh tokens. Per environment: a shared key
    #: would let one environment mint tokens another accepts.
    mcp_token_signing_key: SecretStr | None = None

    #: Session state only -- authorization codes and dynamically registered
    #: clients. Never application data; see ADR-003 and ADR-022.
    mcp_redis_url: str = "redis://localhost:6380/1"

    access_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    refresh_token_ttl_seconds: int = Field(default=86400, ge=300, le=2592000)

    @field_validator("mcp_public_url")
    @classmethod
    def _check_public_url(cls, value: str) -> str:
        """Require an absolute http(s) URL and strip any trailing slash."""
        candidate = value.strip().rstrip("/")
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("MCP_PUBLIC_URL must start with http:// or https://")
        if not candidate.split("://", 1)[1]:
            raise ValueError("MCP_PUBLIC_URL has no host")
        return candidate

    @model_validator(mode="after")
    def _check_oauth_is_configured(self) -> McpSettings:
        """Refuse to start in OAuth mode without the values OAuth needs.

        Checked here rather than at the first request, because the failure mode
        otherwise is a server that accepts connections, sends every client
        through a login, and fails at the callback -- after the user has
        approved something.
        """
        if not self.uses_oauth:
            return self
        missing = [
            name
            for name, value in (
                ("GITHUB_CLIENT_ID", self.github_client_id),
                ("GITHUB_CLIENT_SECRET", self.github_client_secret),
                ("MCP_TOKEN_SIGNING_KEY", self.mcp_token_signing_key),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"MCP_AUTH_MODE=oauth on the http transport requires: {', '.join(missing)}. "
                "Create a GitHub OAuth app, or set MCP_AUTH_MODE=service_token for local use."
            )
        key = self.mcp_token_signing_key
        if key is not None and len(key.get_secret_value()) < _MIN_TOKEN_LENGTH:
            raise ValueError(
                f"MCP_TOKEN_SIGNING_KEY is {len(key.get_secret_value())} characters; "
                f"at least {_MIN_TOKEN_LENGTH} are required. Generate one with "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        if self.refresh_token_ttl_seconds < self.access_token_ttl_seconds:
            raise ValueError(
                "REFRESH_TOKEN_TTL_SECONDS must not be shorter than ACCESS_TOKEN_TTL_SECONDS"
            )
        return self

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

    @property
    def uses_oauth(self) -> bool:
        """True when callers authenticate with OAuth rather than a shared token.

        Only ever true on http. On stdio the process serves the developer who
        launched it, and an OAuth round trip would add a login without adding
        a fact (ADR-014).
        """
        return self.is_http and self.mcp_auth_mode == "oauth"

    @property
    def github_callback_url(self) -> str:
        """Where GitHub sends the user back. Must match the OAuth app exactly."""
        return f"{self.mcp_public_url}/auth/github/callback"


def load_settings() -> McpSettings:
    """Build the settings, reading the environment.

    Not cached. The process builds them once at startup and passes the object
    down, so a cache would only hide where the values come from.
    """
    return McpSettings()  # type: ignore[call-arg]  # values come from the environment

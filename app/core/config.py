"""Application settings, loaded once from the process environment.

Every configurable value in the backend comes from here. Nothing else in the
codebase reads ``os.environ``, so there is exactly one place to look when you
need to know where a value came from or what validates it.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    EmailStr,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "ci", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Substrings that mark a value as a template placeholder rather than a real
# credential. Shipping one of these to a deployed environment is a mistake we
# would rather catch at boot than in an incident.
_PLACEHOLDER_MARKERS = ("replace-with", "change-me", "changeme", "your-tenant", "example.com")

# The only driver this project supports. psycopg3 serves both the async engine
# used by the app and the sync engine used by Alembic, from one URL.
_REQUIRED_DB_SCHEME = "postgresql+psycopg"

# Deployed environments must not run on placeholder credentials.
_STRICT_ENVS: frozenset[str] = frozenset({"staging", "production"})

_MIN_TOKEN_LENGTH = 32


class Settings(BaseSettings):
    """Validated configuration for the FastAPI backend.

    Read this class as the contract between the environment and the code. A
    field here is a promise that the value exists and is well-formed by the
    time any request is served, which is why so much of the validation is
    strict: failing at startup is cheaper than failing per request.

    Secrets are typed ``SecretStr``. That makes ``repr()`` and ``str()`` print
    ``**********``, so a stray log line or traceback cannot leak them. Call
    ``.get_secret_value()`` at the point of use, never earlier.

    Unknown environment variables are ignored, not rejected. The process
    environment legitimately holds hundreds of unrelated variables, and the
    MCP server's own settings (``MCP_*``, ``BACKEND_*``) live in the same
    ``.env`` file. This is the one Pydantic model in the project that does not
    forbid extra input; request models all use ``extra="forbid"``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Secrets are not stripped of whitespace by default; a trailing newline
        # from `echo "token" >> .env` would otherwise break token comparison.
        str_strip_whitespace=True,
    )

    # --- Application -------------------------------------------------------
    app_env: AppEnv = "local"
    app_name: str = "qa-platform"
    api_v1_prefix: str = "/api/v1"
    log_level: LogLevel = "INFO"

    # --- Database ----------------------------------------------------------
    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Echoes every statement to the log. Useful locally, never in a deployment:
    # bound parameters can contain personal data.
    db_echo: bool = False

    # --- Redis -------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    # A readiness probe that hangs is worse than one that fails: Kubernetes
    # would rather hear "not ready" than wait past its own deadline.
    redis_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # --- Jira (Jira Cloud, service account; see ADR-002) -------------------
    jira_base_url: str
    jira_service_email: EmailStr
    jira_api_token: SecretStr
    jira_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    jira_cache_ttl_seconds: int = Field(default=300, ge=0, le=86_400)
    # Jira Cloud has no standard acceptance-criteria field, so this names the
    # custom field your tenant uses, for example "customfield_10040". When it
    # is unset, or set but empty on an issue, the service falls back to
    # parsing an "Acceptance Criteria" heading out of the description.
    jira_acceptance_criteria_field: str | None = None

    # --- Internal auth (see ADR-003) ---------------------------------------
    internal_api_token: SecretStr

    # --- Model access for BRD generation (see ADR-023) ---------------------
    # An OpenAI-compatible chat-completions endpoint. The default is GitHub
    # Models, which every GitHub account can reach, rate limited, at no cost.
    # Changing provider is a change to these three values and nothing else.
    llm_base_url: str = "https://models.github.ai/inference"

    #: Deliberately no default. GitHub Models names models by publisher and the
    #: catalogue changes, so a default would be a guess. List them with
    #: GET https://models.github.ai/catalog/models
    llm_model: str | None = None

    #: A GitHub fine-grained token scoped models:read, for the default
    #: provider. Not the OAuth app secret, and not the Jira token.
    llm_api_key: SecretStr | None = None

    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    llm_max_tokens: int = Field(default=4096, ge=256, le=32768)

    #: Retries are for a timeout, a connection failure, a 429 or a 5xx only.
    #: A 4xx is a request this code built wrongly and will build wrongly again.
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # -----------------------------------------------------------------------
    # Validators
    # -----------------------------------------------------------------------

    @field_validator("api_v1_prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        """Require a leading slash and forbid a trailing one.

        Routes are built by concatenation. Without this, ``api/v1`` silently
        produces relative paths and ``/api/v1/`` produces ``//issues``.
        """
        if not value.startswith("/"):
            raise ValueError("API_V1_PREFIX must start with '/'")
        if len(value) > 1 and value.endswith("/"):
            raise ValueError("API_V1_PREFIX must not end with '/'")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def _check_llm_base_url(cls, value: str) -> str:
        """Require an absolute http(s) URL and strip any trailing slash.

        The client appends ``/chat/completions``, so a trailing slash here
        produces a double slash that some gateways route and others 404.
        """
        candidate = value.strip().rstrip("/")
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL must start with http:// or https://")
        if not candidate.split("://", 1)[1]:
            raise ValueError("LLM_BASE_URL has no host")
        return candidate

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        """Require the ``postgresql+psycopg`` driver explicitly.

        A bare ``postgresql://`` URL makes SQLAlchemy pick psycopg2, which is
        not installed and cannot drive the async engine. Catching it here
        produces a readable error instead of an import failure deep in the
        dialect registry.
        """
        raw = value.get_secret_value()
        scheme = urlsplit(raw).scheme
        if scheme != _REQUIRED_DB_SCHEME:
            # Report the scheme only. The rest of the URL holds the password.
            got = scheme or "no scheme"
            raise ValueError(
                f"DATABASE_URL must use the '{_REQUIRED_DB_SCHEME}' scheme, got '{got}'"
            )
        return value

    @field_validator("jira_base_url")
    @classmethod
    def _check_jira_base_url(cls, value: str) -> str:
        """Require an absolute https URL and drop any trailing slash."""
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("JIRA_BASE_URL must use https")
        if not parts.netloc:
            raise ValueError("JIRA_BASE_URL must include a host, e.g. https://acme.atlassian.net")
        return value.rstrip("/")

    @field_validator("jira_acceptance_criteria_field")
    @classmethod
    def _check_acceptance_criteria_field(cls, value: str | None) -> str | None:
        """Require a Jira custom field id, or nothing.

        Catches the likely mistake of putting the field's display name
        ("Acceptance Criteria") here instead of its id. That would silently
        never match, and the fallback would mask it, so the misconfiguration
        would only surface as "the acceptance criteria are sometimes wrong".
        """
        if value is None or not value.strip():
            return None
        candidate = value.strip()
        if not re.fullmatch(r"customfield_\d+", candidate):
            raise ValueError(
                "JIRA_ACCEPTANCE_CRITERIA_FIELD must be a custom field id like "
                f"'customfield_10040', not {candidate!r}. Find it at "
                "/rest/api/3/field in your Jira tenant."
            )
        return candidate

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: str) -> str:
        """Require a redis scheme, so a wrong value fails here, not on first use."""
        scheme = urlsplit(value).scheme
        if scheme not in {"redis", "rediss", "unix"}:
            raise ValueError("REDIS_URL must start with redis://, rediss:// or unix://")
        return value

    @model_validator(mode="after")
    def _forbid_placeholders_in_deployments(self) -> Self:
        """Refuse to boot a deployed environment on template credentials.

        Local and CI are allowed to run on the values from ``.env.example``,
        because that is the whole point of a template. Staging and production
        are not: a placeholder there means the real secret never arrived.
        """
        if self.app_env not in _STRICT_ENVS:
            return self

        offenders = self.placeholder_secret_fields()
        if offenders:
            raise ValueError(
                f"APP_ENV={self.app_env} but these look like placeholders: "
                f"{', '.join(offenders)}. Supply real values."
            )
        if len(self.internal_api_token.get_secret_value()) < _MIN_TOKEN_LENGTH:
            raise ValueError(
                f"INTERNAL_API_TOKEN must be at least {_MIN_TOKEN_LENGTH} characters "
                f"when APP_ENV={self.app_env}"
            )
        if self.db_echo:
            raise ValueError(f"DB_ECHO must be false when APP_ENV={self.app_env}")
        return self

    # -----------------------------------------------------------------------
    # Derived values
    # -----------------------------------------------------------------------

    def placeholder_secret_fields(self) -> list[str]:
        """Name the settings whose value still looks like a template placeholder.

        Used by the production guard above, and by the startup log in
        ``app/main.py`` so a developer can see at a glance that, for example,
        the Jira token is not real yet.
        """
        candidates: dict[str, str] = {
            "DATABASE_URL": self.database_url.get_secret_value(),
            "JIRA_BASE_URL": self.jira_base_url,
            "JIRA_SERVICE_EMAIL": str(self.jira_service_email),
            "JIRA_API_TOKEN": self.jira_api_token.get_secret_value(),
            "INTERNAL_API_TOKEN": self.internal_api_token.get_secret_value(),
        }
        if self.llm_api_key is not None:
            candidates["LLM_API_KEY"] = self.llm_api_key.get_secret_value()
        return [
            name
            for name, value in candidates.items()
            if any(marker in value.lower() for marker in _PLACEHOLDER_MARKERS)
        ]

    @property
    def generation_available(self) -> bool:
        """True when BRD generation has everything it needs.

        Generation is optional: the backend starts and serves Phase 1 without
        it, and only the BRD endpoints refuse (ADR-023). Checking it here keeps
        the "is it configured" rule in one place rather than in each caller.
        """
        return bool(self.llm_model and self.llm_api_key)

    @property
    def is_deployment(self) -> bool:
        """True for staging and production, where the strict rules apply."""
        return self.app_env in _STRICT_ENVS

    @property
    def database_url_safe(self) -> str:
        """The database URL with the password replaced by ``***``.

        Safe to log, to return from a diagnostic endpoint, and to put in an
        error message. The host, port and database name are the useful parts
        when something cannot connect, and they are not secret.
        """
        parts = urlsplit(self.database_url.get_secret_value())
        if parts.password is None:
            return self.database_url.get_secret_value()
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"{parts.username or ''}:***@{host}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    @property
    def redis_url_safe(self) -> str:
        """The Redis URL with any password replaced by ``***``.

        Most Redis URLs carry no credentials, in which case this returns the
        URL unchanged. It exists so that logging the Redis target is safe by
        construction rather than by the author remembering to check.
        """
        parts = urlsplit(self.redis_url)
        if parts.password is None:
            return self.redis_url
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"{parts.username or ''}:***@{host}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    @property
    def database_url_sync(self) -> str:
        """The same URL for a blocking engine.

        psycopg3 drives both the async engine the application uses and the sync
        engine Alembic uses, so the string is identical. This property exists
        to give Step 5 an explicit, documented name to import rather than
        reaching into the secret.
        """
        return self.database_url.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, building them on first call.

    Cached because validation and ``.env`` parsing should happen once, and
    because FastAPI calls this as a dependency on potentially every request.

    Call ``get_settings.cache_clear()`` in a test that needs to re-read a
    patched environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment


def settings_from_environ(**overrides: object) -> Settings:
    """Build a Settings instance for tests, ignoring the cache and ``.env``.

    Passing explicit overrides keeps a test from depending on whatever happens
    to be in the developer's ``.env`` file.

    Note on email fixtures: ``email-validator`` rejects the reserved TLDs
    ``.test``, ``.invalid``, ``.example`` and ``.localhost``, so an address
    like ``svc@test.invalid`` fails validation. Use a normal-looking domain in
    test data.
    """
    base: dict[str, object] = {
        "database_url": os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://qa:pw@localhost:5432/qa_platform"
        ),
        "jira_base_url": "https://example.atlassian.net",
        "jira_service_email": "svc@acme-qa.co",
        "jira_api_token": "test-token",
        "internal_api_token": "test-internal-token",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def format_validation_error(exc: ValidationError) -> str:
    """Render a settings failure for an operator, without echoing any value.

    ``str(ValidationError)`` appends ``input_value=...`` to every error. For a
    settings model that is a credential leak: a malformed ``DATABASE_URL``
    prints the password, and a too-short ``INTERNAL_API_TOKEN`` prints the
    token. Both end up wherever startup output goes, which in a container is
    the log aggregator.

    So the message is rebuilt from the field name and the rule, which is the
    same approach ``app.core.errors.validation_error_handler`` takes for a
    rejected HTTP request. Found by the Step 8 tests; see ADR-016.

    Every startup path that reports a configuration failure must use this.
    A validator's own message must therefore not quote a secret either —
    the ones in this file report a URL scheme or a length, never a value.
    """
    lines: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        name = location.upper() if location else "configuration"
        # Pydantic prefixes a custom ValueError with "Value error, ", which
        # reads as noise in front of a message written for an operator.
        message = str(error.get("msg", "is invalid")).removeprefix("Value error, ")
        # Most validators above already open with the variable's name, which
        # would otherwise be printed twice.
        message = message.removeprefix(f"{name} ")
        lines.append(f"  {name}: {message}")
    return "\n".join(lines) if lines else "  configuration is invalid"

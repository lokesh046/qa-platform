"""FastAPI application assembly.

This module is wiring only. It owns no business logic and should stay short
enough to read in one screen: a reader who wants to know what this service is
made of should be able to find out here.

Order matters in two places, both marked below.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import ValidationError

from app import __version__
from app.core.config import Settings, format_validation_error, get_settings
from app.core.database import dispose_engine
from app.core.errors import register_exception_handlers
from app.core.logging import RequestIdMiddleware, configure_logging
from app.core.redis import close_redis
from app.modules.audit.router import router as audit_router
from app.modules.health.router import router as health_router
from app.modules.jira.client import close_http_client
from app.modules.jira.router import router as jira_router

_logger = logging.getLogger(__name__)


def _log_startup_summary(settings: Settings) -> None:
    """Record what this process was configured with, and complain if needed.

    Deliberately explicit about which values are safe to print. Secrets are
    never included, not even partially: a prefix is enough to shorten a brute
    force, and a log line is the easiest thing in a system to leak.

    Placeholder secrets are reported by name only. In ``local`` they are
    expected, so this is an INFO-level note; in a deployment
    ``Settings`` already refuses to construct, so this line never prints there.
    """
    _logger.info(
        "application starting",
        extra={
            "version": __version__,
            "app_env": settings.app_env,
            "api_prefix": settings.api_v1_prefix,
            "log_level": settings.log_level,
            "database": settings.database_url_safe,
            "redis": settings.redis_url_safe,
            "jira_base_url": settings.jira_base_url,
        },
    )
    placeholders = settings.placeholder_secret_fields()
    if placeholders:
        _logger.info(
            "placeholder secrets in use; calls to real services will fail",
            extra={"placeholder_fields": sorted(placeholders)},
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup and shutdown work around the serving period.

    Startup does not open a database connection. A service that refuses to
    start because Postgres is briefly unavailable cannot report *why* through
    ``/health``, and under an orchestrator it enters a restart loop. The
    engine is lazy, ``/ready`` reports the truth, and the orchestrator holds
    traffic back until it passes.

    Shutdown releases the database pool, the Redis pool and the Jira HTTP
    pool. Without it, a reload leaves connections open until the other side
    times them out, which during rapid development can exhaust Postgres's
    ``max_connections``.
    """
    _log_startup_summary(app.state.settings)
    try:
        yield
    finally:
        await dispose_engine()
        await close_redis()
        await close_http_client()
        _logger.info("application stopped")


def create_app() -> FastAPI:
    """Build and wire the application.

    Takes no settings argument on purpose, even though a factory that accepted
    one would look more testable. It would not be: the database engine, the
    Redis client and the ``get_settings`` dependency all resolve the
    process-wide cached settings themselves, so an injected object would reach
    the startup log line and nothing else. An app configured one way that
    serves requests another way is worse than no injection at all.

    To run against different configuration, change the environment and clear
    the caches::

        get_settings.cache_clear()
        reset_engine_cache()
        reset_redis_cache()

    Making settings genuinely per-app means threading them through those
    factories. Phase 1 does not need it; see the Step 4 log.

    Raises:
        RuntimeError: The environment is not valid. Raised in place of the
            ``ValidationError``, because that exception's string form appends
            ``input_value=...`` for every rejected field — which for this
            model means printing the database password into the startup log.
            ``from None`` suppresses the original for the same reason. See
            ADR-016.
    """
    try:
        settings = get_settings()
    except ValidationError as exc:
        raise RuntimeError(
            "The backend cannot start: its configuration is not valid.\n"
            f"{format_validation_error(exc)}\n"
            "Values come from the environment, or from .env. "
            "Copy .env.example and fill it in."
        ) from None

    # Before anything else logs, so startup lines are JSON like the rest.
    configure_logging(level=settings.log_level, service=settings.app_name)

    app = FastAPI(
        title="AI QA Automation Platform API",
        version=__version__,
        summary="Internal backend. All business logic lives here; the MCP server only forwards.",
        lifespan=lifespan,
        # Interactive docs are useful internally and harmless: every endpoint
        # that returns data requires a token. Revisit if this is ever exposed
        # beyond the cluster.
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # Outermost middleware. It must wrap the exception handlers, not sit inside
    # them, or a request that fails before routing gets no request id and no
    # access log line.
    app.add_middleware(RequestIdMiddleware)

    register_exception_handlers(app)
    app.include_router(health_router, prefix=settings.api_v1_prefix)
    app.include_router(jira_router, prefix=settings.api_v1_prefix)
    app.include_router(audit_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()

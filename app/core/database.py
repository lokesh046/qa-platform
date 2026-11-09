"""Database engine, session factory, and the declarative base.

The engine is created on first use rather than at import, so importing any
module in this package does not open a socket. That keeps unit tests and
``--help`` fast, and it means a misconfigured database fails at the first
request rather than at interpreter startup.

The async engine is driven by psycopg3. The same URL also drives a blocking
engine, which is what Alembic uses in Step 5; see ADR-006 for why the
application side is async.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, get_settings

_logger = logging.getLogger("app.database")


#: Deterministic names for every constraint and index.
#:
#: Without this, Postgres invents names like ``users_email_key`` and Alembic
#: autogenerates ``op.create_unique_constraint(None, ...)``. A later migration
#: that wants to drop that constraint then has nothing to name, and the
#: downgrade cannot be written at all. Set once, before the first table exists:
#: changing it afterwards renames every constraint in the database.
_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model in this project.

    The models in ``app/models/`` inherit from this. It lives here rather than
    in ``app/models/`` because the metadata is infrastructure: Alembic needs
    it, and a model package that imported the engine would create an import
    cycle.
    """

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call.

    Pool sizing comes from settings. ``pool_pre_ping`` costs one cheap round
    trip per checkout and in exchange survives the common case of Postgres
    closing an idle connection, which otherwise surfaces as a random
    ``OperationalError`` on an unrelated request.
    """
    settings = get_settings()
    _logger.info("creating database engine", extra={"database": settings.database_url_safe})
    return create_async_engine(
        settings.database_url.get_secret_value(),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory.

    ``expire_on_commit=False`` is deliberate. With the default, every attribute
    of every instance is invalidated on commit, so touching a field afterwards
    triggers a fresh SELECT — and in async code that lazy load raises
    ``MissingGreenlet`` instead of quietly working. Keeping instances usable
    after commit avoids a class of confusing failures.
    """
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session for the duration of one request. FastAPI dependency.

    The transaction is rolled back if the handler raises, and the session is
    always closed. Committing is left to the service layer, because only the
    service knows what a complete unit of work is.

    Usage in a router::

        async def read_issue(session: Annotated[AsyncSession, Depends(get_session)]) -> ...:
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database_connection() -> bool:
    """Return True if a trivial query succeeds.

    Used by ``/ready``. Deliberately a real round trip: a pool that holds a
    dead connection looks healthy until something asks it a question.
    """
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        # The caller turns this into a 503. The reason is logged, not returned,
        # because a connection error message can include the host and user.
        _logger.exception("database readiness check failed")
        return False
    return True


async def dispose_engine() -> None:
    """Close every pooled connection. Called on application shutdown.

    Without this, a reload or a shutdown leaves connections open until Postgres
    times them out, which during rapid development can exhaust
    ``max_connections``.
    """
    if get_engine.cache_info().currsize == 0:
        return
    await get_engine().dispose()
    _logger.info("database engine disposed")


def reset_engine_cache() -> None:
    """Drop the cached engine and session factory.

    For tests that point the application at a different database. Call
    ``dispose_engine()`` first if an engine may already be open.
    """
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def sync_database_url(settings: Settings | None = None) -> str:
    """Return the blocking-driver URL, for Alembic and one-off scripts.

    psycopg3 serves both the async and the sync engine, so this is the same
    string the async engine uses. Named explicitly so Step 5 does not have to
    reach into a ``SecretStr``.
    """
    return (settings or get_settings()).database_url_sync

"""Alembic migration environment.

Uses a **blocking** engine, not the application's async one. Migrations are a
serial batch job, so async buys nothing and costs an event-loop wrapper around
every revision. ADR-006 records that decision.

The database URL is never read from alembic.ini. It comes from
``app.core.config``, which reads the environment, so there is one place that
knows the credentials and no password in a committed file.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Importing the package registers every model on Base.metadata. Without this
# import --autogenerate sees an empty schema and cheerfully writes a migration
# that drops all five tables.
import app.models  # noqa: F401  # imported for the side effect on Base.metadata
from app.core.config import get_settings
from app.core.database import Base, sync_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL, failing with a readable message rather than a traceback.

    A missing ``.env`` is the most common way this breaks, and the default
    Pydantic validation error is a wall of text that does not mention the file.
    """
    try:
        return sync_database_url()
    except Exception as exc:  # operator-facing, must stay readable
        sys.stderr.write(
            "Could not build the database URL from the environment.\n"
            "Copy .env.example to .env and fill in DATABASE_URL.\n"
            f"Underlying error: {exc}\n"
        )
        raise SystemExit(1) from exc


def _safe_url_for_logging() -> str:
    """The URL with the password masked, for the one line we print."""
    return get_settings().database_url_safe


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    ``alembic upgrade head --sql`` produces a script for a DBA to review and
    apply by hand, which is how a production change should go out.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Catch a column type that changed, not just added and dropped
        # columns. The migrations README warns that Alembic misses type
        # narrowing by default; these two options are that warning's fix.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run the migrations against the live database."""
    sys.stderr.write(f"alembic target: {_safe_url_for_logging()}\n")
    engine = create_engine(
        _database_url(),
        # No pooling: this process runs a handful of statements and exits.
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Wrap each migration in its own transaction, so a failure halfway
            # through a multi-revision upgrade leaves the database at a
            # recorded revision rather than somewhere undefined.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

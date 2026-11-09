"""Shared column definitions for the ORM models.

Mixins rather than a deeper base class hierarchy. A model needs a primary key
and usually needs timestamps, but ``audit_logs`` wants a different key type and
``user_project_access`` has no meaningful ``updated_at``. Composing small
mixins expresses that; one ``BaseModel`` carrying everything would not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

#: Reused by every timestamp column. ``timezone=True`` maps to Postgres
#: ``timestamptz``, which stores an absolute instant. A naive ``timestamp``
#: column silently records the server's local clock, and the first time a
#: container runs in another timezone the data stops being orderable.
TimestampTZ = DateTime(timezone=True)


class UUIDPrimaryKeyMixin:
    """A ``uuid`` primary key, generated on both sides.

    ``server_default=gen_random_uuid()`` means a row inserted by hand in
    ``psql``, or by a future data migration, still gets a valid id. The
    function is built into PostgreSQL 13 and later, so no extension is needed.

    ``default=uuid.uuid4`` is also set, Python-side, so the ORM knows the id
    *before* the INSERT. Without it, code that needs the new id has to flush
    and refetch.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
        # Mixin columns are otherwise appended after the model's own, putting
        # the primary key last in every CREATE TABLE and every `\d` output.
        sort_order=-100,
    )


class CreatedAtMixin:
    """``created_at`` only, for rows that are created and never edited."""

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ,
        nullable=False,
        server_default=func.now(),
        sort_order=100,
    )


class TimestampMixin(CreatedAtMixin):
    """``created_at`` and ``updated_at``, both database-defaulted.

    Caveat worth knowing before you trust ``updated_at``: it is refreshed by
    SQLAlchemy's ``onupdate``, which is Python-side. A raw SQL ``UPDATE``, or a
    bulk ``update()`` that bypasses the ORM, leaves it stale. Making it
    airtight needs a database trigger; ADR-008 records why Phase 1 does not
    have one.
    """

    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        # After created_at. Without an explicit order, TimestampMixin's own
        # column sorts before the one it inherits.
        sort_order=101,
    )


def enum_column(enum: type[StrEnum], name: str, length: int) -> Enum:
    """Build a ``VARCHAR`` + ``CHECK`` column type for a string enum.

    ``native_enum=False`` is the point: a real Postgres ``ENUM`` cannot gain a
    value inside a transactional migration, and renaming one is worse. See
    ADR-008.

    ``values_callable`` makes the stored text the member *value*
    (``"qa_engineer"``), not the member *name* (``"QA_ENGINEER"``), which is
    SQLAlchemy's default and surprises everyone exactly once.
    """
    return Enum(
        enum,
        name=name,
        native_enum=False,
        # Without this the column is a bare VARCHAR and the database accepts
        # any string at all. SQLAlchemy has defaulted create_constraint to
        # False since 1.4, so `native_enum=False` alone buys no validation
        # whatever -- which defeats the entire reason for choosing VARCHAR
        # plus CHECK over a native enum. Found by the Step 5 verification,
        # which inserted role='superuser' successfully.
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
        length=length,
    )

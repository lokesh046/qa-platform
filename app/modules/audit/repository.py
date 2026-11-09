"""Database writes for the audit trail.

Insert only. There is deliberately no update and no delete: the table is
append-only, and the absence of those functions is the only enforcement of that
in Phase 1.
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.user import User
from app.modules.audit.schemas import AuditEvent


async def insert_event(
    session: AsyncSession,
    event: AuditEvent,
    *,
    actor_user_id: uuid.UUID | None,
    request_id: str | None,
) -> None:
    """Append one audit row.

    A Core-level ``insert`` rather than an ORM object: nothing needs the row
    back, and this avoids loading it into the session's identity map for no
    reason.

    ``occurred_at`` is left to the column's ``server_default now()``, so the
    database sets the time and no caller can backdate a record.
    """
    await session.execute(
        insert(AuditLog).values(
            actor_email=event.actor_email,
            actor_user_id=actor_user_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome,
            source=event.source,
            detail=event.detail,
            request_id=request_id,
        )
    )


async def resolve_actor_user_id(session: AsyncSession, email: str) -> uuid.UUID | None:
    """Find the user id for an email, or None if there is no such user.

    Duplicates the lookup in ``app.modules.jira.repository`` rather than
    importing it. That is deliberate: the audit trail must keep working when
    any other module is broken or removed, so it does not depend on one.
    """
    stmt = select(User.id).where(User.email == email.lower())
    return (await session.execute(stmt)).scalar_one_or_none()

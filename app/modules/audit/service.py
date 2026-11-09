"""Writing audit records.

One function, :func:`record`, and the important thing about it is the session.

**It opens its own session and commits it independently.** Using the caller's
session would be wrong in a way that is easy to miss and would quietly defeat
the purpose of the table: ``app.core.database.get_session`` rolls back on
exception, so a denial recorded in the request's transaction would be rolled
back by the very ``NotFoundError`` it exists to record. The rows that matter
most -- denials and errors -- would be the only ones never written.

The cost is a second connection checkout per audited request. See the Step 6
addendum for the pool arithmetic.
"""

from __future__ import annotations

import logging
import uuid

from app.core.database import get_session_factory
from app.core.logging import get_request_id
from app.modules.audit import repository
from app.modules.audit.schemas import MAX_RESOURCE_ID_LENGTH, AuditEvent

_logger = logging.getLogger("app.audit")


async def record(event: AuditEvent, *, actor_user_id: uuid.UUID | None = None) -> None:
    """Append one audit record, in its own transaction.

    Failures are **not** swallowed. A gatekeeper that cannot record what it did
    should not quietly carry on: if this raises, the request fails and no data
    is returned. That is the fail-closed direction, and because every outcome
    is audited through the same path, a database problem produces the same
    failure for an allowed read and a denied one -- so ADR-010's rule that the
    two are indistinguishable still holds.

    Args:
        event: What happened. Validated, with scalar-only ``detail`` values.
        actor_user_id: The user's primary key when known. Nullable because the
            actor may not be a registered user, which is itself an audited
            outcome.
    """
    async with get_session_factory()() as session:
        try:
            await repository.insert_event(
                session, event, actor_user_id=actor_user_id, request_id=get_request_id()
            )
            await session.commit()
        except Exception:
            await session.rollback()
            # Logged here as well as propagating, because the exception the
            # caller sees becomes a generic 500 with no detail by design.
            _logger.exception(
                "failed to write audit record",
                extra={"action": event.action, "outcome": event.outcome.value},
            )
            raise


def truncate_resource_id(value: str) -> str:
    """Shorten a resource id to the column width.

    Truncating rather than rejecting: an over-long id means something upstream
    is wrong, and losing the audit record is worse than recording a clipped
    identifier.
    """
    if len(value) <= MAX_RESOURCE_ID_LENGTH:
        return value
    return value[: MAX_RESOURCE_ID_LENGTH - 1] + "~"

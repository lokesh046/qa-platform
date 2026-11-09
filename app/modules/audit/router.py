"""The audit write endpoint.

Exists so the MCP server can record its own tool calls without holding
database credentials. A gatekeeper whose compromise reaches the database is not
much of a gatekeeper, so the backend keeps the credentials and exposes this one
narrow write instead. See ADR-013.

There is no read endpoint. Reading the trail is an administrator's job, done
with SQL, until something genuinely needs it over HTTP.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.errors import ResponseEnvelope, success_payload
from app.core.logging import get_request_id
from app.core.security import Actor, require_actor
from app.models.audit_log import AuditSource
from app.modules.audit import repository
from app.modules.audit.schemas import AuditEvent, AuditWriteRequest, AuditWriteResponse

_logger = logging.getLogger("app.audit")

router = APIRouter(tags=["audit"])


@router.post(
    "/audit",
    response_model=ResponseEnvelope[AuditWriteResponse],
    summary="Append one audit record",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid service token."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Unknown field, or an action outside the 'mcp.' namespace."
        },
    },
)
async def write_audit_record(
    body: AuditWriteRequest,
    actor: Annotated[Actor, Depends(require_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Record one thing a gatekeeper did.

    Four fields are taken from the server, never the body, because a caller
    able to set them could forge the trail:

    - **actor** comes from the ``X-Actor-Email`` header, the same value the
      rest of the API authorises against;
    - **source** is always ``mcp``, so a caller cannot write a row that claims
      to have come from the backend itself;
    - **occurred_at** is the column's ``server_default now()``, so a record
      cannot be backdated;
    - **request_id** comes from this request's own context, which
      ``RequestIdMiddleware`` has already sanitised, so a caller cannot inject
      a forged or malformed correlation id.

    ``action`` is constrained to the ``mcp.`` namespace for the same reason:
    without it, anything holding the service token could append a convincing
    ``jira_issue.read`` row for a read that never happened.
    """
    actor_user_id = await repository.resolve_actor_user_id(session, actor.email)
    event = AuditEvent(
        actor_email=actor.email,
        action=body.action,
        outcome=body.outcome,
        source=AuditSource.MCP,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        detail=body.detail,
    )
    await repository.insert_event(
        session, event, actor_user_id=actor_user_id, request_id=get_request_id()
    )
    await session.commit()
    _logger.info(
        "audit record appended",
        extra={"action": event.action, "outcome": event.outcome.value, "source": "mcp"},
    )
    return success_payload(AuditWriteResponse(recorded=True))

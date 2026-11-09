"""HTTP endpoints for generated requirements documents.

Thin: validate, resolve the actor, call the service, wrap the result. Every
decision about who may generate what lives in ``service.py``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.errors import ResponseEnvelope, success_payload
from app.core.security import Actor, require_actor
from app.models.jira_issue import ISSUE_KEY_PATTERN
from app.modules.brd.schemas import BrdGenerateRequest, BrdRead
from app.modules.brd.service import generate, read_current

router = APIRouter(tags=["brd"])

_SHARED_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid service token."},
    status.HTTP_403_FORBIDDEN: {"description": "Actor is unknown or deactivated."},
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "The issue does not exist, or the actor has no access to its project. "
            "These are deliberately indistinguishable."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Malformed issue key or body."},
}


@router.post(
    "/brd",
    response_model=ResponseEnvelope[BrdRead],
    summary="Generate a requirements document for an issue",
    status_code=status.HTTP_200_OK,
    responses={
        **_SHARED_RESPONSES,
        status.HTTP_502_BAD_GATEWAY: {
            "description": (
                "The model provider failed, or returned something that is not a usable "
                "requirements document."
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Generation is not configured on this server."
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {"description": "The model did not answer in time."},
    },
)
async def generate_brd(
    request: Annotated[BrdGenerateRequest, Body()],
    actor: Annotated[Actor, Depends(require_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Write requirements for one Jira issue, in OpenSpec's format.

    Returns 200 rather than 201 even when a document is created, because the
    common case is a document that already existed and was returned unchanged.
    A status that depended on that would make the caller branch on it; the
    ``reused_existing`` field says which happened.

    Reads the issue through the normal access gates first, so nothing reaches a
    model on behalf of a caller who would have been refused.
    """
    document = await generate(session, actor, request.issue_key, regenerate=request.regenerate)
    return success_payload(document)


@router.get(
    "/brd/{issue_key}",
    response_model=ResponseEnvelope[BrdRead],
    summary="Read the current requirements document for an issue",
    status_code=status.HTTP_200_OK,
    responses=_SHARED_RESPONSES,
)
async def read_brd(
    issue_key: Annotated[
        str,
        Path(
            pattern=ISSUE_KEY_PATTERN,
            min_length=3,
            max_length=64,
            description="Uppercase project key, a hyphen, then digits. For example ABC-123.",
            examples=["ABC-123"],
        ),
    ],
    actor: Annotated[Actor, Depends(require_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return the newest document for an issue without generating one.

    404 when no document exists yet, with a message saying to generate one —
    the same status as "you may not see this project", because distinguishing
    them would leak which projects exist (ADR-010).
    """
    document = await read_current(session, actor, issue_key)
    return success_payload(document)

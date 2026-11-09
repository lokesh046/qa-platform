"""HTTP endpoints for reading Jira issues.

Thin: validate the path parameter, resolve the actor, call the service, wrap
the result. Every decision about who may see what lives in ``service.py``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.errors import ResponseEnvelope, success_payload
from app.core.security import Actor, require_actor
from app.models.jira_issue import ISSUE_KEY_PATTERN
from app.modules.jira.schemas import IssueRead
from app.modules.jira.service import get_issue

router = APIRouter(tags=["jira"])


@router.get(
    "/issues/{issue_key}",
    response_model=ResponseEnvelope[IssueRead],
    summary="Read one Jira issue",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid service token."},
        status.HTTP_403_FORBIDDEN: {"description": "Actor is unknown or deactivated."},
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "The issue does not exist, or the actor has no access to its project. "
                "These are deliberately indistinguishable."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Malformed issue key."},
        status.HTTP_502_BAD_GATEWAY: {"description": "Jira refused or failed."},
        status.HTTP_504_GATEWAY_TIMEOUT: {"description": "Jira did not answer in time."},
    },
)
async def read_issue(
    issue_key: Annotated[
        str,
        Path(
            # Rejected by FastAPI before any handler code runs, so a malformed
            # key never reaches the database or Jira.
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
    """Return one issue's summary, description and acceptance criteria.

    Served from the local cache when the cached copy is within
    ``JIRA_CACHE_TTL_SECONDS``, otherwise fetched from Jira and cached. The
    ``from_cache`` field in the response says which happened.
    """
    issue = await get_issue(session, actor, issue_key)
    return success_payload(issue)

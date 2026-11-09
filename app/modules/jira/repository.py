"""Database queries for the jira module.

The only place in this module that touches SQLAlchemy. Functions here take a
session and return models or primitives; they never commit, because deciding
the transaction boundary is the service's job.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jira_issue import JiraIssue
from app.models.user import User, UserProjectAccess


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Find a user by email, or None.

    The email is lowercased before comparison. ``users.email`` is stored
    lowercase and a CHECK enforces it, so comparing a mixed-case address
    without normalising would simply never match.
    """
    stmt = select(User).where(User.email == email.lower())
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_project_access(session: AsyncSession, user_id: uuid.UUID, project_key: str) -> bool:
    """True if this user holds a grant for this project.

    Selects the primary key rather than the row: nothing needs the grant's
    contents, only whether one exists. Served by the
    ``uq_user_project_access_user_id_project_key`` unique index.
    """
    stmt = select(UserProjectAccess.id).where(
        UserProjectAccess.user_id == user_id,
        UserProjectAccess.project_key == project_key,
    )
    return (await session.execute(stmt)).first() is not None


async def get_cached_issue(session: AsyncSession, issue_key: str) -> JiraIssue | None:
    """Return the cached row for an issue, or None."""
    stmt = select(JiraIssue).where(JiraIssue.issue_key == issue_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_issue(
    session: AsyncSession,
    *,
    issue_key: str,
    summary: str,
    description: str | None,
    acceptance_criteria: str | None,
    issue_type: str | None,
    status: str | None,
    jira_updated_at: datetime | None,
    fetched_at: datetime,
) -> JiraIssue:
    """Insert or refresh the cached copy of an issue, and return it.

    A single ``INSERT ... ON CONFLICT DO UPDATE`` rather than a select-then-
    insert-or-update. Two concurrent requests for the same uncached issue is
    the normal case, not an edge case — a user asks, and the MCP server asks on
    their behalf — and the read-then-write version loses that race with a
    unique-constraint violation.

    ``project_key`` is absent from both the insert and the update: it is a
    generated column, and Postgres rejects any attempt to write it. That is
    deliberate, because it feeds the permission check.
    """
    values = {
        "issue_key": issue_key,
        "summary": summary,
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "issue_type": issue_type,
        "status": status,
        "jira_updated_at": jira_updated_at,
        "fetched_at": fetched_at,
    }
    stmt = (
        insert(JiraIssue)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[JiraIssue.issue_key],
            # Everything except the key itself. updated_at is refreshed
            # explicitly because ORM-level onupdate does not fire for a
            # Core-level INSERT ... ON CONFLICT; see ADR-008.
            set_={
                "summary": values["summary"],
                "description": values["description"],
                "acceptance_criteria": values["acceptance_criteria"],
                "issue_type": values["issue_type"],
                "status": values["status"],
                "jira_updated_at": values["jira_updated_at"],
                "fetched_at": values["fetched_at"],
                "updated_at": fetched_at,
            },
        )
        .returning(JiraIssue)
    )
    return (await session.execute(stmt)).scalar_one()

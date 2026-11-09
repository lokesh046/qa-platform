"""Cached Jira issue data.

A read-through cache, not a source of truth. Jira owns these fields; this table
holds the subset the platform needs so that repeated reads of the same issue do
not hammer the Jira REST API and so a Jira outage does not immediately break
every feature that needs an issue summary.

What is deliberately *not* here: the raw Jira issue payload. See ADR-009.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Computed, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, TimestampTZ, UUIDPrimaryKeyMixin

#: A Jira issue key, for example ``ABC-123``. Identical to the pattern the MCP
#: tool validates in Step 7. Kept in sync by hand; see the Step 5 log.
ISSUE_KEY_PATTERN = r"^[A-Z][A-Z0-9]+-[0-9]+$"

_ISSUE_KEY_LENGTH = 64
_PROJECT_KEY_LENGTH = 32


class JiraIssue(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cached Jira issue.

    ``fetched_at`` is what Step 6 compares against ``JIRA_CACHE_TTL_SECONDS``
    to decide whether to serve this row or call Jira again. It is distinct from
    ``updated_at``, which records when *this row* changed: a refetch that
    returns identical data still moves ``fetched_at``, and that is the correct
    behaviour for a TTL.
    """

    __tablename__ = "jira_issues"

    issue_key: Mapped[str] = mapped_column(String(_ISSUE_KEY_LENGTH), nullable=False, unique=True)
    # Derived by the database from issue_key rather than written by the
    # application. A stored generated column cannot drift, which matters here
    # because the project key is what the permission check in Step 6 compares
    # against user_project_access. A mismatch between the two would be an
    # authorisation bug, not a display bug.
    project_key: Mapped[str] = mapped_column(
        String(_PROJECT_KEY_LENGTH),
        Computed("split_part(issue_key, '-', 1)", persisted=True),
        nullable=False,
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Jira's description and the acceptance criteria field both arrive as
    # Atlassian Document Format. Step 6 flattens them to plain text before
    # storing, so these columns hold text a human can read.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)

    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Jira's own "updated" timestamp, not ours. Lets a future conditional
    # refetch skip work when Jira says nothing changed.
    jira_updated_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)

    __table_args__ = (
        CheckConstraint(f"issue_key ~ '{ISSUE_KEY_PATTERN}'", name="issue_key_format"),
        CheckConstraint("length(summary) > 0", name="summary_not_empty"),
        # Serves "every cached issue in project ABC".
        Index("ix_jira_issues_project_key", "project_key"),
        # Serves cache eviction and "what is stale?" queries.
        Index("ix_jira_issues_fetched_at", "fetched_at"),
    )

    def __repr__(self) -> str:
        """Identify the issue by key only."""
        return f"<JiraIssue {self.issue_key}>"

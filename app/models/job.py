"""Background job records.

Phase 1 creates no jobs. The table exists now because the first migration is
the cheapest one to get right, and because Step 7's audit records and Phase 2's
BRD generation both reference a job id. Writing it later would mean a migration
against a table other things already point at.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import (
    TimestampMixin,
    TimestampTZ,
    UUIDPrimaryKeyMixin,
    enum_column,
)

_ISSUE_KEY_LENGTH = 64


class JobType(StrEnum):
    """What kind of work a job represents.

    Every member except ``JIRA_REFRESH`` belongs to a later phase. They are
    listed now so the ``CHECK`` constraint does not need widening in Phase 2;
    adding a value to a ``VARCHAR`` + ``CHECK`` column is a migration either
    way, but doing it once is better than three times.
    """

    JIRA_REFRESH = "jira_refresh"
    BRD_GENERATE = "brd_generate"
    TEST_GENERATE = "test_generate"
    TEST_EXECUTE = "test_execute"
    DEFECT_REPORT = "defect_report"


class JobStatus(StrEnum):
    """Where a job is in its lifecycle.

    ``QUEUED`` and ``RUNNING`` are live; the other three are terminal. The
    distinction is enforced by a ``CHECK`` constraint tying terminal status to
    ``finished_at``.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The statuses that mean the job will not change again. Kept as a tuple so the
#: CHECK constraint below and future service code read the same list.
TERMINAL_STATUSES: tuple[JobStatus, ...] = (
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
)

_TERMINAL_SQL_LIST = ", ".join(f"'{status.value}'" for status in TERMINAL_STATUSES)


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One unit of asynchronous work.

    The id is handed back to the caller so they can poll, which is why it is a
    UUID rather than a sequence: a sequential job id tells an unrelated user
    how much work the platform has done, and is guessable.
    """

    __tablename__ = "jobs"

    job_type: Mapped[JobType] = mapped_column(enum_column(JobType, "job_type", 32), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, "job_status", 16),
        nullable=False,
        server_default=JobStatus.QUEUED.value,
    )

    # SET NULL rather than CASCADE: deleting a user must not erase the record
    # that work was done, which is the same reasoning as audit_logs.
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Denormalised so a job stays attributable after the user row is gone.
    requested_by_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # The issue this job is about, when it is about one. Not a foreign key to
    # jira_issues: a job may be queued for an issue that has never been
    # cached, and the cache is evictable.
    issue_key: Mapped[str | None] = mapped_column(String(_ISSUE_KEY_LENGTH), nullable=True)

    # Inputs and outputs. JSONB rather than separate columns because the shape
    # differs per job_type and Phase 1 cannot know the later shapes.
    # Never put a secret in either: these are read by anything that can read
    # the jobs table, and they end up in support exports.
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Caller-safe failure text, the same string the API would return. The
    # traceback goes to the log, not here. See app/core/errors.py.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))

    started_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    __table_args__ = (
        # A terminal job has a finish time and a live one does not. Without
        # this, a crashed worker leaves rows that look running forever and
        # rows that look finished with no timestamp, and no query can tell
        # which. Postgres evaluates this on every write, so the invariant
        # cannot be broken by a bug in a worker.
        CheckConstraint(
            f"(status IN ({_TERMINAL_SQL_LIST})) = (finished_at IS NOT NULL)",
            name="terminal_status_has_finished_at",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NOT NULL",
            name="finished_implies_started",
        ),
        # Serves the worker's "what should I pick up next?" query.
        Index("ix_jobs_status_created_at", "status", "created_at"),
        Index("ix_jobs_issue_key", "issue_key"),
    )

    def __repr__(self) -> str:
        """Identify the job by id, type and status."""
        return f"<Job {self.id} {self.job_type}/{self.status}>"

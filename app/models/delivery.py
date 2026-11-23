"""Proposed Jira work, the test cases that prove it, and the scripts that run.

Everything here is *proposed* until a person approves it. A Jira item with no
``jira_key`` has not been written to Jira; one with a key has, and the
idempotency key is what stops a retry creating it twice.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, TimestampTZ, UUIDPrimaryKeyMixin


class JiraItemKind(StrEnum):
    """What sort of Jira issue an item will become."""

    STORY = "story"
    TASK = "task"
    SUBTASK = "subtask"


class ItemStatus(StrEnum):
    """Where a proposed item is.

    ``PROPOSED`` has been generated; ``APPROVED`` may be written to Jira;
    ``REJECTED`` never will be; ``CREATED`` exists in Jira.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CREATED = "created"


_KIND_SQL = ", ".join(f"'{k.value}'" for k in JiraItemKind)
_ITEM_STATUS_SQL = ", ".join(f"'{s.value}'" for s in ItemStatus)


class JiraItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One proposed story, task or subtask."""

    __tablename__ = "jira_items"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    #: Which requirement this implements. Nullable only because a task may
    #: serve several; the common case names one, and an item serving none is
    #: a sign the breakdown drifted from the document.
    requirement_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: A subtask's parent task, or a task's parent story.
    parent_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jira_items.id", ondelete="CASCADE"), nullable=True
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ItemStatus.PROPOSED.value,
        server_default=ItemStatus.PROPOSED.value,
    )

    #: Sent to Jira so a retry after a timeout cannot create a second issue.
    #: Unique across the table, because that is the guarantee it has to make.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Set once the issue exists in Jira.
    jira_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_in_jira_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint(f"kind IN ({_KIND_SQL})", name="jira_item_kind"),
        CheckConstraint(f"status IN ({_ITEM_STATUS_SQL})", name="jira_item_status"),
        CheckConstraint("length(summary) > 0", name="summary_not_empty"),
        CheckConstraint("phase IS NULL OR phase >= 1", name="phase_positive"),
        # A story has no parent; a subtask must have one. Postgres enforces
        # it, so a malformed hierarchy cannot be written to Jira.
        CheckConstraint(
            f"(kind = '{JiraItemKind.STORY.value}') = (parent_item_id IS NULL)",
            name="only_stories_are_roots",
        ),
        # Existing in Jira and having a key are the same fact.
        CheckConstraint(
            f"(status = '{ItemStatus.CREATED.value}')"
            " = (jira_key IS NOT NULL AND created_in_jira_at IS NOT NULL)",
            name="created_has_jira_key",
        ),
        Index("uq_jira_items_idempotency_key", "idempotency_key", unique=True),
        Index("ix_jira_items_document_id", "document_id"),
        Index("ix_jira_items_requirement_id", "requirement_id"),
        Index("ix_jira_items_parent_item_id", "parent_item_id"),
    )

    def __repr__(self) -> str:
        """Identify the item by kind and Jira key if it has one."""
        return f"<JiraItem {self.kind} {self.jira_key or self.status}>"


class TestCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One test case: what to do, with what data, and what should happen."""

    __tablename__ = "test_cases"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    scenario_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True
    )
    jira_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jira_items.id", ondelete="SET NULL"), nullable=True
    )

    ref: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    #: Numbered steps as text. A list of rows would be tidier and would mean
    #: the text a reviewer approved is not the text that is stored.
    steps: Mapped[str] = mapped_column(Text, nullable=False)
    test_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ItemStatus.PROPOSED.value,
        server_default=ItemStatus.PROPOSED.value,
    )
    jira_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint(f"status IN ({_ITEM_STATUS_SQL})", name="test_case_status"),
        CheckConstraint("length(steps) > 0", name="steps_not_empty"),
        CheckConstraint("length(expected_result) > 0", name="expected_result_not_empty"),
        Index("uq_test_cases_document_ref", "document_id", "ref", unique=True),
        Index("uq_test_cases_idempotency_key", "idempotency_key", unique=True),
        Index("ix_test_cases_requirement_id", "requirement_id"),
    )

    def __repr__(self) -> str:
        """Identify the test case by its reference."""
        return f"<TestCase {self.ref}>"


class ScriptStatus(StrEnum):
    """Where a generated script is.

    ``GENERATED`` exists but has not been linted; ``LINT_FAILED`` did not
    pass; ``READY`` passed and awaits review; ``APPROVED`` has been reviewed.
    """

    GENERATED = "generated"
    LINT_FAILED = "lint_failed"
    READY = "ready"
    APPROVED = "approved"
    REJECTED = "rejected"


_SCRIPT_STATUS_SQL = ", ".join(f"'{s.value}'" for s in ScriptStatus)


class Script(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One generated automation script for one test case."""

    __tablename__ = "scripts"

    test_case_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False
    )

    #: Path within the repository, which is where the script actually lives.
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(
        String(32), nullable=False, default="python", server_default="python"
    )
    framework: Mapped[str] = mapped_column(
        String(32), nullable=False, default="playwright", server_default="playwright"
    )

    #: Which template produced it, so a template fix can find everything it
    #: produced.
    template_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ScriptStatus.GENERATED.value,
        server_default=ScriptStatus.GENERATED.value,
    )

    #: What the linter said. Kept whatever the outcome: a passing run is
    #: evidence, and a failing one is what somebody has to fix.
    lint_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (
        CheckConstraint(f"status IN ({_SCRIPT_STATUS_SQL})", name="script_status"),
        CheckConstraint("length(file_path) > 0", name="file_path_not_empty"),
        CheckConstraint(
            "git_commit IS NULL OR git_commit ~ '^[0-9a-f]{40}$'", name="git_commit_full_sha"
        ),
        # A failed lint has to say why. Otherwise the status is an assertion
        # nobody can check.
        CheckConstraint(
            f"(status <> '{ScriptStatus.LINT_FAILED.value}') OR (lint_output IS NOT NULL)",
            name="lint_failed_has_output",
        ),
        Index("ix_scripts_test_case_id", "test_case_id"),
    )

    def __repr__(self) -> str:
        """Identify the script by path and status."""
        return f"<Script {self.file_path} {self.status}>"

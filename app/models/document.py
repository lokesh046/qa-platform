"""Generated BRD and FDS documents, and the structure parsed out of them.

A document is a version of one artifact. Approving it does not edit it:
corrections create the next version, so what somebody approved stays readable.

The text lives in Git, and ``git_commit`` records which commit holds it. The
row is the index — who wrote it, which version, what state it is in, and what
it was derived from — because Git answers none of those questions cheaply.
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

_EMAIL_LENGTH = 320


class DocumentType(StrEnum):
    """Which artifact a document is.

    ``BRD`` is the business requirements document; ``FDS`` is the functional
    design derived from an approved BRD.
    """

    BRD = "brd"
    FDS = "fds"


class DocumentStatus(StrEnum):
    """Where a document version is in its review.

    ``DRAFT`` has been generated but not shown for review. ``IN_REVIEW`` is
    waiting on a person. ``APPROVED`` may be built on; ``REJECTED`` may not;
    ``SUPERSEDED`` is an older version of the same artifact.
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


_TYPE_SQL = ", ".join(f"'{t.value}'" for t in DocumentType)
_STATUS_SQL = ", ".join(f"'{s.value}'" for s in DocumentStatus)


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One version of one BRD or FDS."""

    __tablename__ = "documents"

    document_type: Mapped[str] = mapped_column(String(8), nullable=False)

    #: Groups every version of one artifact. Version 2 of a BRD shares this
    #: with version 1, which is what makes "the current BRD" a query rather
    #: than a flag two writers could both set.
    lineage_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DocumentStatus.DRAFT.value,
        server_default=DocumentStatus.DRAFT.value,
    )

    #: The Jira project this work belongs to. Not an authorisation boundary --
    #: Jira decides that -- but it scopes lookups and groups a chain.
    project_key: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Optional. A chain may start from a Jira issue or from pasted use-case
    #: text, so this is null whenever it started from text.
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: The use case as the user gave it. Kept verbatim: it is the input a
    #: reviewer compares the document against.
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: An FDS names the approved BRD it was derived from. Null on a BRD.
    source_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )

    #: The OpenSpec change directory holding this document's files, relative
    #: to openspec/changes/.
    openspec_change: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: The commit holding this version's files. Null until it is committed.
    git_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)

    #: Identifies the prompt that produced it, so "why does this read like
    #: that" stays answerable.
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_reported: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Denormalised, as on audit_logs: the record must survive the user row
    #: being deleted. Lowercased, per ADR-017.
    author_email: Mapped[str] = mapped_column(String(_EMAIL_LENGTH), nullable=False)
    author_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    finished_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    __table_args__ = (
        CheckConstraint(f"document_type IN ({_TYPE_SQL})", name="document_type"),
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="document_status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("author_email = lower(author_email)", name="author_email_lowercase"),
        # A commit is a full sha or nothing. A short sha is ambiguous, and an
        # ambiguous reference to the text somebody approved is worthless.
        CheckConstraint(
            "git_commit IS NULL OR git_commit ~ '^[0-9a-f]{40}$'", name="git_commit_full_sha"
        ),
        # An FDS has a source; a BRD does not. Enforced here because the
        # alternative is an FDS nobody can trace to a BRD.
        CheckConstraint(
            f"(document_type = '{DocumentType.FDS.value}') = (source_document_id IS NOT NULL)",
            name="fds_has_source",
        ),
        CheckConstraint("issue_key IS NOT NULL OR source_text IS NOT NULL", name="has_an_input"),
        # One version number per artifact.
        Index("uq_documents_lineage_version", "lineage_id", "version", unique=True),
        Index("ix_documents_lineage_id", "lineage_id"),
        Index("ix_documents_project_key", "project_key"),
        Index("ix_documents_author_email_created_at", "author_email", "created_at"),
    )

    def __repr__(self) -> str:
        """Identify the document by type, version and status."""
        return f"<Document {self.document_type} v{self.version} {self.status}>"


class Requirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One requirement inside a document.

    Given its own row rather than left in the markdown because everything
    downstream points at it: a Jira story, a test case and a script all cite a
    requirement id, and "which tests cover REQ-4" must be a query.
    """

    __tablename__ = "requirements"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    #: Stable within a document, e.g. REQ-1. Quoted by stories, test cases and
    #: scripts, so it is the identifier a human uses in conversation.
    ref: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    #: One sentence using SHALL or MUST. Detail belongs in the scenarios.
    statement: Mapped[str] = mapped_column(Text, nullable=False)

    #: Delivery phase of the product being specified, not of this platform.
    #: Travels from the FDS into the stories derived from it.
    phase: Mapped[int | None] = mapped_column(Integer, nullable=True)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("uq_requirements_document_ref", "document_id", "ref", unique=True),
        CheckConstraint("length(ref) > 0", name="ref_not_empty"),
        CheckConstraint("length(statement) > 0", name="statement_not_empty"),
        CheckConstraint("phase IS NULL OR phase >= 1", name="phase_positive"),
        Index("ix_requirements_document_id", "document_id"),
    )

    def __repr__(self) -> str:
        """Identify the requirement by its reference."""
        return f"<Requirement {self.ref}>"


class Scenario(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One WHEN/THEN case under a requirement.

    The unit a reviewer approves or rejects individually, and the unit a test
    case is written from.
    """

    __tablename__ = "scenarios"

    requirement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False
    )

    ref: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    #: Kept as text rather than a list of steps: a reviewer approves prose,
    #: and splitting it would mean the approved text and the stored text could
    #: differ.
    given_when: Mapped[str] = mapped_column(Text, nullable=False)
    then_outcome: Mapped[str] = mapped_column(Text, nullable=False)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("uq_scenarios_requirement_ref", "requirement_id", "ref", unique=True),
        CheckConstraint("length(given_when) > 0", name="when_not_empty"),
        CheckConstraint("length(then_outcome) > 0", name="then_not_empty"),
        Index("ix_scenarios_requirement_id", "requirement_id"),
    )

    def __repr__(self) -> str:
        """Identify the scenario by its reference."""
        return f"<Scenario {self.ref}>"


class OpenQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Something the source did not say, which a person must answer.

    An unanswered question blocks approval. That is the mechanism that stops
    the model inventing a business rule to fill a gap: it has somewhere to put
    the gap instead.
    """

    __tablename__ = "open_questions"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    answered_by_email: Mapped[str | None] = mapped_column(String(_EMAIL_LENGTH), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("length(question) > 0", name="question_not_empty"),
        # Answered means all three together, so a half-recorded answer cannot
        # silently unblock an approval.
        CheckConstraint(
            "(answer IS NULL) = (answered_at IS NULL)"
            " AND (answer IS NULL) = (answered_by_email IS NULL)",
            name="answer_is_complete",
        ),
        CheckConstraint(
            "answered_by_email IS NULL OR answered_by_email = lower(answered_by_email)",
            name="answered_by_lowercase",
        ),
        Index("ix_open_questions_document_id", "document_id"),
    )

    def __repr__(self) -> str:
        """Identify the question by whether it is answered."""
        return f"<OpenQuestion answered={self.answer is not None}>"

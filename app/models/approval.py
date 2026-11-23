"""Recorded human decisions.

The row that makes "no Jira write and no script run without recorded human
approval" checkable rather than asserted. It is written when a person decides,
names what they decided about and which version, and is never updated: a
change of mind is a new decision, so the sequence stays readable.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin

_EMAIL_LENGTH = 320


class ApprovalTarget(StrEnum):
    """What kind of thing was decided about."""

    DOCUMENT = "document"
    SCENARIO = "scenario"
    JIRA_ITEMS = "jira_items"
    TEST_CASES = "test_cases"
    SCRIPT = "script"


class ApprovalDecision(StrEnum):
    """What was decided.

    ``CHANGES_REQUESTED`` is distinct from ``REJECTED``: the first expects a
    new version, the second does not.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


_TARGET_SQL = ", ".join(f"'{t.value}'" for t in ApprovalTarget)
_DECISION_SQL = ", ".join(f"'{d.value}'" for d in ApprovalDecision)


class Approval(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One decision a person made.

    Append-only: no ``updated_at``, and nothing updates a row. Changing your
    mind writes another decision, which is the honest record of what happened.
    """

    __tablename__ = "approvals"

    target_type: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Deliberately not a foreign key. The targets live in five tables, and a
    #: constraint per table would mean five nullable columns of which exactly
    #: one may be set. The integrity that matters -- that the decision names
    #: a version -- is in target_version below.
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    #: Which version was decided on. An approval that does not name a version
    #: cannot prove anything once the text changes.
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)

    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Denormalised so the record survives the user row being deleted.
    actor_email: Mapped[str] = mapped_column(String(_EMAIL_LENGTH), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(f"target_type IN ({_TARGET_SQL})", name="approval_target_type"),
        CheckConstraint(f"decision IN ({_DECISION_SQL})", name="approval_decision"),
        CheckConstraint("target_version >= 1", name="target_version_positive"),
        CheckConstraint("actor_email = lower(actor_email)", name="actor_email_lowercase"),
        # A rejection or a request for changes must say why. An approval need
        # not: "yes" is complete on its own.
        CheckConstraint(
            f"decision = '{ApprovalDecision.APPROVED.value}' OR note IS NOT NULL",
            name="refusal_has_a_note",
        ),
        Index("ix_approvals_target", "target_type", "target_id", "target_version"),
        Index("ix_approvals_actor_email_created_at", "actor_email", "created_at"),
    )

    def __repr__(self) -> str:
        """Identify the decision by what it was about and what it was."""
        return f"<Approval {self.target_type} {self.decision}>"

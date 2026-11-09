"""Users and their per-project access grants."""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_column

#: A Jira project key: uppercase letters and digits, starting with a letter.
#: The same shape the MCP tool validates in Step 7, without the issue number.
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9]+$"

#: Jira caps project keys at 10 characters. The slack is headroom, not a use
#: case. 320 is the practical maximum length of an email address.
_PROJECT_KEY_LENGTH = 32
_EMAIL_LENGTH = 320


class UserRole(StrEnum):
    """What a user may do platform-wide.

    Separate from project access, which is per-project and lives in
    :class:`UserProjectAccess`. A ``QA_LEAD`` with no grant for project ABC
    still cannot read ABC-1.

    Phase 1 stores the role but enforces only project access. The approval
    steps that read it arrive in Phase 3.
    """

    VIEWER = "viewer"
    QA_ENGINEER = "qa_engineer"
    QA_LEAD = "qa_lead"
    ADMIN = "admin"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person who can use the platform, identified by their work email.

    The email is the identity that arrives from Copilot Chat through the MCP
    server's actor header, so it is the natural external key. It is stored
    lowercase and a ``CHECK`` enforces that, because two rows differing only in
    case would be two accounts for one person.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(_EMAIL_LENGTH), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "user_role", 16),
        nullable=False,
        server_default=UserRole.VIEWER.value,
    )
    # Deactivating rather than deleting keeps audit rows and job history
    # attributable. Step 6 refuses a request from an inactive user.
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    project_access: Mapped[list[UserProjectAccess]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="UserProjectAccess.user_id",
        # Eager loading must be explicit under the async engine: a lazy load
        # outside a greenlet context raises MissingGreenlet rather than
        # quietly issuing a query. "raise" turns that into an error at the
        # call site instead of in production. See ADR-006.
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("position('@' in email) > 1", name="email_has_local_part"),
    )

    def __repr__(self) -> str:
        """Identify the row without printing anything else about the person."""
        return f"<User {self.email}>"


class UserProjectAccess(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A grant letting one user read one Jira project.

    This is the authorisation check in front of every Jira read. Step 6 looks
    for a row here before calling Jira, so a user cannot borrow the service
    account's wider permissions to reach a project they were never given.

    No ``updated_at``: a grant is created or revoked, never edited, so the
    column would always equal ``created_at`` and invite the reader to believe
    otherwise.
    """

    __tablename__ = "user_project_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        # Deleting a user revokes their access, which is the safe direction.
        # The record of what they did lives in audit_logs, which does not
        # cascade.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_key: Mapped[str] = mapped_column(String(_PROJECT_KEY_LENGTH), nullable=False)
    # Who granted it. Nullable because a bootstrap script has no user behind
    # it, and SET NULL so removing an admin does not revoke everyone's access.
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    user: Mapped[User] = relationship(
        back_populates="project_access",
        foreign_keys=[user_id],
        lazy="raise",
    )

    __table_args__ = (
        # One grant per user per project, and the index Step 6's permission
        # lookup reads. Names come from the metadata naming convention.
        UniqueConstraint("user_id", "project_key"),
        CheckConstraint(f"project_key ~ '{PROJECT_KEY_PATTERN}'", name="project_key_format"),
        # Answers "who can see project ABC?", which the constraint above cannot
        # serve because project_key is not its leading column.
        Index("ix_user_project_access_project_key", "project_key"),
    )

    def __repr__(self) -> str:
        """Identify the grant."""
        return f"<UserProjectAccess {self.user_id} -> {self.project_key}>"

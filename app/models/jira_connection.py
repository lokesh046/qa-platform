"""Per-user Jira OAuth connections.

One row per person who has connected their Jira account. Every Jira call is
made with that person's own token, so Jira enforces their permissions and this
platform holds no shared service credential.

The refresh token is stored encrypted. The column holds ciphertext produced by
the service layer; nothing here can decrypt it, and no query returns it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, TimestampTZ, UUIDPrimaryKeyMixin


class JiraConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One person's connection to one Jira site."""

    __tablename__ = "jira_connections"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    #: Atlassian's identifier for the site, used to build every API URL.
    cloud_id: Mapped[str] = mapped_column(String(64), nullable=False)
    site_url: Mapped[str] = mapped_column(String(512), nullable=False)

    #: The Atlassian account this token belongs to. Recorded so a connection
    #: can be tied back to a person in Jira's own terms, not just ours.
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    #: Ciphertext. Bytes rather than text so no encoding step can lose a byte,
    #: and so a careless str() cannot produce something that looks readable.
    refresh_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: Which key version encrypted the above, so a key can be rotated without
    #: guessing which rows are still on the old one.
    key_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")

    #: Space-separated scopes granted at consent. Stored so a scope added
    #: later can be detected as missing without a failed API call.
    scopes: Mapped[str] = mapped_column(String(512), nullable=False)

    #: When the current access token expires. The access token itself is never
    #: stored; it lives in memory for the life of a request.
    access_expires_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    #: Set when a refresh fails, so the user is asked to connect again instead
    #: of every call failing with the same upstream error.
    revoked_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)

    __table_args__ = (
        # One connection per user per site. Reconnecting updates the row
        # rather than accumulating tokens nobody can tell apart.
        Index("uq_jira_connections_user_cloud", "user_id", "cloud_id", unique=True),
        CheckConstraint("length(cloud_id) > 0", name="cloud_id_not_empty"),
        CheckConstraint("length(refresh_token_encrypted) > 0", name="token_not_empty"),
        CheckConstraint("key_version >= 1", name="key_version_positive"),
        CheckConstraint(
            "account_email IS NULL OR account_email = lower(account_email)",
            name="account_email_lowercase",
        ),
        Index("ix_jira_connections_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        """Identify the connection without revealing anything secret."""
        return f"<JiraConnection user={self.user_id} site={self.site_url}>"

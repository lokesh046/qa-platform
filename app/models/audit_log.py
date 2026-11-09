"""Append-only audit trail.

Every MCP tool call writes a row here, which is the main reason the MCP server
exists as a separate gatekeeper at all. The table answers "who asked for what,
when, and were they allowed?" and it has to keep answering after the user, the
job and the cached issue are all gone.

Three design rules follow from that:

1. **Append-only.** Nothing in this project updates or deletes an audit row.
2. **Self-contained.** The actor's email and the action are stored as text, not
   only as a foreign key, so a deleted user does not erase the history.
3. **No secrets, ever.** ``detail`` is free-form JSON; see ADR-009 for what may
   and may not go in it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampTZ, enum_column


class AuditOutcome(StrEnum):
    """Whether the attempt succeeded, was refused, or broke.

    ``DENIED`` is the valuable one. A run of denials for one actor is the
    signal that someone is probing for projects they cannot see.
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


class AuditSource(StrEnum):
    """Which surface the request arrived through.

    Lets a reader tell a Copilot Chat tool call apart from a direct HTTP call
    to the backend, which matters when the two have different trust levels.
    """

    MCP = "mcp"
    API = "api"


class AuditLog(Base):
    """One recorded attempt to do something.

    The primary key is a ``bigint`` identity, not a UUID like every other
    table here. This table is append-only and almost always read in time order,
    so a monotonic key keeps inserts at the right-hand edge of the index
    instead of scattering them across it. Nothing external ever quotes an audit
    id, so there is no enumeration concern to trade against. See ADR-008.
    """

    __tablename__ = "audit_logs"

    # Identity rather than BIGSERIAL: an identity column is standard SQL and
    # owns its sequence, so there is no separate object to grant on or to
    # forget when restoring. "by default" rather than "always" so a restore or
    # a data migration can still carry explicit ids.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)

    # Set by the database, not the caller. An actor must not be able to
    # backdate their own audit row.
    occurred_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )

    # The request_id from app/core/logging.py, so a row here can be joined to
    # the JSON log lines for the same request.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Denormalised on purpose: see rule 2 in the module docstring.
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Dotted verb, for example "jira_issue.read" or "mcp.get_jira_issue".
    # Free text rather than an enum: a new capability should not need a
    # migration to be auditable, and an unparseable action is better than an
    # unrecorded one.
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    outcome: Mapped[AuditOutcome] = mapped_column(
        enum_column(AuditOutcome, "audit_outcome", 16), nullable=False
    )
    source: Mapped[AuditSource] = mapped_column(
        enum_column(AuditSource, "audit_source", 16), nullable=False
    )

    # Why a request was denied, which validation rule rejected an argument,
    # how long an upstream call took. Never a token, a password, a full request
    # body or a full Jira payload. ADR-009 has the rule.
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        # "What has this person been doing?" — the most common audit query.
        Index("ix_audit_logs_actor_email_occurred_at", "actor_email", "occurred_at"),
        # "Show me every denial" — cheap because denials are the rare value.
        Index(
            "ix_audit_logs_outcome_occurred_at",
            "outcome",
            "occurred_at",
            postgresql_where=text("outcome <> 'allowed'"),
        ),
        # "What happened to issue ABC-1?"
        Index("ix_audit_logs_resource_type_resource_id", "resource_type", "resource_id"),
        Index("ix_audit_logs_occurred_at", "occurred_at"),
    )

    def __repr__(self) -> str:
        """Identify the row by actor, action and outcome."""
        return f"<AuditLog {self.actor_email} {self.action} {self.outcome}>"

"""Shapes for audit records.

``audit_logs.detail`` is schemaless JSONB, which ADR-009 identifies as the most
likely place a secret will eventually be written. These models are the start of
fixing that by construction rather than by review: a caller passes a typed
event, not a free-form dict.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.audit_log import AuditOutcome, AuditSource

#: Actions accepted by ``POST /api/v1/audit``. Namespaced to ``mcp.`` so a
#: gatekeeper cannot write rows that look like the backend's own
#: (``jira_issue.read``). Without this, anything holding the service token
#: could forge a record of a read that never happened.
MCP_ACTION_PATTERN = r"^mcp\.[a-z0-9_]+(\.[a-z0-9_]+)*$"

#: Longest resource id recorded. Matches the column width. An id longer than
#: this is truncated rather than rejected: failing to audit is worse than
#: auditing imprecisely.
MAX_RESOURCE_ID_LENGTH = 128


class AuditEvent(BaseModel):
    """One thing that happened, ready to be written.

    ``extra="forbid"`` so a caller cannot smuggle an unreviewed field into the
    audit trail by adding a keyword argument.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_email: str = Field(
        max_length=320, description="Who acted. Kept even if the user row is later deleted."
    )
    action: str = Field(max_length=64, description="Dotted verb, for example 'jira_issue.read'.")
    outcome: AuditOutcome = Field(description="allowed, denied, or error.")
    source: AuditSource = Field(description="Which surface the request arrived through.")
    resource_type: str | None = Field(default=None, max_length=32)
    resource_id: str | None = Field(default=None, max_length=MAX_RESOURCE_ID_LENGTH)
    detail: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        description=(
            "Why this outcome happened. Values are scalars only, so a payload, "
            "a body or a nested object cannot be passed. Never a credential; see ADR-009."
        ),
    )

    @field_validator("actor_email")
    @classmethod
    def _canonical_email(cls, value: str) -> str:
        """Lowercase the actor, so one person is one actor in the trail.

        Belt and braces with ``app.core.security.parse_actor``, which already
        canonicalises what arrives over HTTP. This covers a row written from
        anywhere else — a CLI, a future job — because a trail where the same
        user appears as three actors cannot answer "what did they do?".
        See ADR-017.
        """
        return value.strip().lower()


class AuditWriteRequest(BaseModel):
    """Body of ``POST /api/v1/audit``.

    Note what is **absent**: the actor, the source, the timestamp and the
    request id. All four are set by the server, because all four are things a
    caller would otherwise be able to forge. The body carries only what
    happened, never who it happened to.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        pattern=MCP_ACTION_PATTERN,
        max_length=64,
        description="Dotted verb, which must begin with 'mcp.'. For example 'mcp.get_jira_issue'.",
    )
    outcome: AuditOutcome = Field(description="allowed, denied, or error.")
    resource_type: str | None = Field(default=None, max_length=32)
    resource_id: str | None = Field(default=None, max_length=MAX_RESOURCE_ID_LENGTH)
    detail: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        description="Scalar values only. Never a credential or a payload; see ADR-009.",
    )


class AuditWriteResponse(BaseModel):
    """Confirmation that a record was appended.

    Deliberately carries no audit id. ADR-008 chose a sequential key for this
    table on the grounds that nothing external quotes it; returning it here
    would make that false and would leak how much the platform has done.
    """

    model_config = ConfigDict(extra="forbid")

    recorded: bool = Field(description="Always true. A failure is an error response, not false.")

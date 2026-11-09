"""Request and response models for the jira module."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.jira_issue import ISSUE_KEY_PATTERN


class IssueRead(BaseModel):
    """One issue, as this API returns it.

    A deliberately small projection of a Jira issue. ADR-009 explains what is
    missing and why: no reporter, no assignee, no comments, no attachments, no
    custom fields beyond acceptance criteria.
    """

    model_config = ConfigDict(extra="forbid")

    issue_key: str = Field(description="The Jira issue key, for example ABC-123.")
    project_key: str = Field(description="The project the issue belongs to, derived from the key.")
    summary: str = Field(description="The issue's one-line summary.")
    description: str | None = Field(
        default=None, description="The description, flattened from ADF to plain text."
    )
    acceptance_criteria: str | None = Field(
        default=None,
        description=(
            "Acceptance criteria, from the configured custom field or, failing that, "
            "from an 'Acceptance Criteria' heading in the description. Null when neither exists."
        ),
    )
    issue_type: str | None = Field(default=None, description="For example Bug or Story.")
    status: str | None = Field(default=None, description="For example 'To Do' or 'In Progress'.")
    jira_updated_at: datetime | None = Field(
        default=None, description="When Jira last changed the issue."
    )
    fetched_at: datetime = Field(description="When this service last read the issue from Jira.")
    from_cache: bool = Field(
        description="True if served from the local cache without calling Jira."
    )


class IssueKeyPath(BaseModel):
    """Validator for the ``issue_key`` path parameter.

    The router also declares the pattern, so FastAPI rejects a malformed key
    before any handler code runs. This model exists for the service layer,
    which is called by the MCP server in Step 7 as well as by the router, and
    must not rely on having been called through HTTP.
    """

    model_config = ConfigDict(extra="forbid")

    issue_key: str = Field(
        pattern=ISSUE_KEY_PATTERN,
        min_length=3,
        max_length=64,
        description="Uppercase project key, a hyphen, then digits. For example ABC-123.",
    )

"""Tool argument validation.

The MCP server's main job. An argument that reaches the backend has been
checked here first, so a malformed key never costs a backend request, a
database query or a Jira call.

Validation is duplicated between here and the backend on purpose. The backend
cannot trust this process — anything could call it with the service token — so
it validates again. Belt and braces, with the constraint stated once in
``app.models.jira_issue`` and imported by both.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.jira_issue import ISSUE_KEY_PATTERN


class GetJiraIssueArgs(BaseModel):
    """Arguments for the ``get_jira_issue`` tool.

    ``extra="forbid"`` so a model that invents a plausible extra argument gets
    a clear error rather than having it silently dropped. That matters more
    here than elsewhere: the caller is a language model, and a silently ignored
    argument becomes a confidently wrong answer.
    """

    model_config = ConfigDict(extra="forbid")

    issue_key: str = Field(
        pattern=ISSUE_KEY_PATTERN,
        min_length=3,
        max_length=64,
        description="A Jira issue key: uppercase project key, a hyphen, then digits, e.g. ABC-123.",
    )

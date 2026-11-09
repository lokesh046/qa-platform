"""Request and response models for BRD generation.

Input models reject unknown fields. A caller that sends ``regenrate`` should be
told, not silently given the default — and the caller here is often a language
model filling in a tool schema, which makes a typo likelier than usual.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.brd_document import BrdStatus
from app.models.jira_issue import ISSUE_KEY_PATTERN


class BrdGenerateRequest(BaseModel):
    """Ask for a BRD for one issue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_key: str = Field(
        pattern=ISSUE_KEY_PATTERN,
        min_length=3,
        max_length=64,
        description="The Jira issue to write requirements for, for example ABC-123.",
        examples=["ABC-123"],
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "Generate a new document even if one already exists. Off by "
            "default, because generating costs money and the existing "
            "document is usually what the caller wanted."
        ),
    )


class BrdRead(BaseModel):
    """A generated document, or the record of an attempt that produced none."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    issue_key: str
    project_key: str
    status: BrdStatus = Field(
        description=(
            "ready: a document with requirements. needs_info: the issue said "
            "too little, so the document is open questions only. failed: no "
            "document was produced. pending: generation has not finished."
        )
    )
    markdown: str | None = Field(
        default=None,
        description="The document, in OpenSpec's format. Null when the attempt produced none.",
    )
    requirement_count: int | None = None
    scenario_count: int | None = None
    open_question_count: int | None = None
    prompt_version: str = Field(description="Identifies the prompt that produced this document.")
    model_reported: str | None = Field(
        default=None,
        description="The model the provider said answered. Not necessarily the one requested.",
    )
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    failure_reason: str | None = Field(
        default=None, description="Short reason a failed attempt failed. Never the model's words."
    )
    created_at: datetime
    finished_at: datetime | None = None
    reused_existing: bool = Field(
        default=False,
        description=(
            "True when an existing document was returned instead of generating "
            "a new one. Pass regenerate=true to force a new generation."
        ),
    )

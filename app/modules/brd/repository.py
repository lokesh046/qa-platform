"""Database queries for generated BRDs.

Queries only. Every decision about who may generate what is in ``service.py``;
nothing here checks an actor's access, so nothing here may be called without
that check having happened first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brd_document import BrdDocument, BrdStatus


async def get_current_document(session: AsyncSession, issue_key: str) -> BrdDocument | None:
    """Return the newest document for an issue, or ``None``.

    "Current" is the newest row whose status says it holds a document, which is
    the query ADR-024 chose over an ``is_current`` flag two writers could both
    set. ``needs_info`` counts: open questions are the document in that case,
    and returning nothing would make the platform look like it had not tried.
    """
    result = await session.execute(
        select(BrdDocument)
        .where(
            BrdDocument.issue_key == issue_key,
            BrdDocument.status.in_([BrdStatus.READY.value, BrdStatus.NEEDS_INFO.value]),
        )
        .order_by(desc(BrdDocument.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_document_by_id(session: AsyncSession, document_id: uuid.UUID) -> BrdDocument | None:
    """Return one document by id, whatever its status."""
    result = await session.execute(select(BrdDocument).where(BrdDocument.id == document_id))
    return result.scalar_one_or_none()


async def open_attempt(
    session: AsyncSession,
    *,
    issue_key: str,
    prompt_version: str,
    actor_email: str,
    actor_user_id: uuid.UUID | None,
) -> BrdDocument:
    """Insert a ``pending`` row and return it.

    Written **before** the model is called, so a generation that never comes
    back leaves a visible row rather than nothing at all. The partial index on
    pending rows exists to find those.

    Does not commit. The caller commits, because it decides whether the pending
    row should be durable before the model call.
    """
    document = BrdDocument(
        issue_key=issue_key,
        status=BrdStatus.PENDING.value,
        prompt_version=prompt_version,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
    )
    session.add(document)
    await session.flush()
    return document


async def complete_attempt(
    session: AsyncSession,
    document: BrdDocument,
    *,
    markdown: str,
    status: BrdStatus,
    requirement_count: int,
    scenario_count: int,
    open_question_count: int,
    model_reported: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> BrdDocument:
    """Record a successful attempt against its pending row.

    ``status`` is ``READY`` or ``NEEDS_INFO``; both carry markdown, which the
    ``document_status_has_markdown`` CHECK constraint enforces regardless of
    what this function is passed.
    """
    document.status = status.value
    document.markdown = markdown
    document.requirement_count = requirement_count
    document.scenario_count = scenario_count
    document.open_question_count = open_question_count
    document.model_reported = model_reported
    document.prompt_tokens = prompt_tokens
    document.completion_tokens = completion_tokens
    document.finished_at = datetime.now(UTC)
    await session.flush()
    return document


async def fail_attempt(
    session: AsyncSession,
    document: BrdDocument,
    *,
    reason: str,
    model_reported: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> BrdDocument:
    """Record a failed attempt, keeping any token usage it already incurred.

    Usage is kept on a failure on purpose: a reply that was unusable was still
    charged for, and a cost report that only counts successes understates spend
    exactly when something is going wrong.

    ``reason`` is a short code, never a traceback and never the model's own
    words — the reply is untrusted text (ADR-023) and this column is read by
    people and by SQL.
    """
    document.status = BrdStatus.FAILED.value
    document.failure_reason = reason[:64]
    document.model_reported = model_reported
    document.prompt_tokens = prompt_tokens
    document.completion_tokens = completion_tokens
    document.finished_at = datetime.now(UTC)
    await session.flush()
    return document

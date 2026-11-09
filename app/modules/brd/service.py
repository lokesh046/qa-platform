"""Turning a Jira issue into a Business Requirements Document.

The order of operations here is the module's security property, exactly as in
``modules/jira/service.py``:

1. the four access gates, by **reusing** ``jira.service.get_issue``;
2. is generation configured at all;
3. is there already a document, unless the caller asked for a new one;
4. open a pending row, **then** call the model;
5. validate the reply, then store it or record the failure.

Gate 1 is delegated rather than reimplemented. Generating a BRD needs the
issue, and getting the issue already means "registered, active, has a grant on
this project, cache or Jira" — four checks in a fixed order that exist in one
place. A second copy here would be a second place for that order to be wrong,
and the order is the part that matters: nothing reaches a model on behalf of a
caller who was going to be refused.

A consequence worth knowing: a generation writes **two** audit rows, one
``jira_issue.read`` from the delegated call and one ``brd.generate`` from here.
That is correct — two things happened — and it is why both carry the same
``resource_id``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError, NotFoundError, ServiceUnavailableError, UpstreamError
from app.core.security import Actor
from app.llm import client as llm
from app.llm.openspec_format import validate
from app.llm.prompt import build_brd_prompt
from app.models.audit_log import AuditOutcome, AuditSource
from app.models.brd_document import BrdDocument, BrdStatus
from app.models.user import User
from app.modules.audit.schemas import AuditEvent
from app.modules.audit.service import record as record_audit
from app.modules.brd import repository
from app.modules.brd.schemas import BrdRead
from app.modules.jira import repository as jira_repository
from app.modules.jira.schemas import IssueKeyPath, IssueRead
from app.modules.jira.service import get_issue

_logger = logging.getLogger("app.brd.service")

GENERATE_ACTION = "brd.generate"
READ_ACTION = "brd.read"
_RESOURCE_TYPE = "brd_document"


async def _audit(
    actor: Actor,
    outcome: AuditOutcome,
    *,
    action: str,
    issue_key: str | None,
    reason: str | None = None,
    user: User | None = None,
    source: AuditSource = AuditSource.API,
    **detail: str | int | float | bool | None,
) -> None:
    """Record one generation or read attempt.

    Local to this module for the same reason the jira service keeps its own:
    every exit path then reads as one line, which is what makes it checkable at
    a glance that no path is missing one.
    """
    payload: dict[str, str | int | float | bool | None] = dict(detail)
    if reason is not None:
        payload["reason"] = reason
    await record_audit(
        AuditEvent(
            actor_email=actor.email,
            action=action,
            outcome=outcome,
            source=source,
            resource_type=_RESOURCE_TYPE,
            resource_id=issue_key,
            detail=payload,
        ),
        actor_user_id=user.id if user is not None else None,
    )


def _to_read(document: BrdDocument, *, reused_existing: bool = False) -> BrdRead:
    """Map a row to its API representation."""
    return BrdRead(
        id=document.id,
        issue_key=document.issue_key,
        project_key=document.project_key,
        status=BrdStatus(document.status),
        markdown=document.markdown,
        requirement_count=document.requirement_count,
        scenario_count=document.scenario_count,
        open_question_count=document.open_question_count,
        prompt_version=document.prompt_version,
        model_reported=document.model_reported,
        prompt_tokens=document.prompt_tokens,
        completion_tokens=document.completion_tokens,
        failure_reason=document.failure_reason,
        created_at=document.created_at,
        finished_at=document.finished_at,
        reused_existing=reused_existing,
    )


async def _gated_issue(
    session: AsyncSession,
    actor: Actor,
    issue_key: str,
    *,
    action: str,
    settings: Settings,
    source: AuditSource,
) -> IssueRead:
    """Run the access gates by delegating, auditing a refusal under *this* action.

    The delegated call audits its own refusal as ``jira_issue.read``, which is
    true but not the whole truth: somebody tried to *generate a document* and
    was refused, and a trail that only records the read makes "who tried to
    generate for ABC-1" unanswerable. Found by a live call during Step 12.

    So one refusal produces two rows, which is accurate -- the generation was
    refused, and the read it required was refused.
    """
    try:
        return await get_issue(session, actor, issue_key, settings=settings, source=source)
    except AppError as exc:
        await _audit(
            actor,
            AuditOutcome.DENIED if exc.status_code in {403, 404} else AuditOutcome.ERROR,
            action=action,
            issue_key=issue_key,
            reason=exc.code.value,
            source=source,
        )
        raise


async def read_current(
    session: AsyncSession,
    actor: Actor,
    raw_issue_key: str,
    *,
    settings: Settings | None = None,
    source: AuditSource = AuditSource.API,
) -> BrdRead:
    """Return the current document for an issue, without generating one.

    Runs the same access gates as generating, by the same delegation, so a
    caller cannot read a document for a project they have no grant on. The
    delegated call may fetch the issue from Jira, which is wasteful for a pure
    read of our own table — and the alternative is a second copy of the gate
    order, which is the thing most worth not duplicating.

    Raises:
        NotFoundError: No document exists, or the actor may not see the
            project. Deliberately indistinguishable, as in ADR-010.
    """
    settings = settings or get_settings()
    issue_key = IssueKeyPath(issue_key=raw_issue_key).issue_key

    await _gated_issue(
        session, actor, issue_key, action=READ_ACTION, settings=settings, source=source
    )
    user = await jira_repository.get_user_by_email(session, actor.email)

    document = await repository.get_current_document(session, issue_key)
    if document is None:
        await _audit(
            actor,
            AuditOutcome.ERROR,
            action=READ_ACTION,
            issue_key=issue_key,
            reason="no_document",
            user=user,
            source=source,
        )
        raise NotFoundError(
            f"No requirements document exists for {issue_key} yet. Generate one first."
        )

    await _audit(
        actor,
        AuditOutcome.ALLOWED,
        action=READ_ACTION,
        issue_key=issue_key,
        user=user,
        source=source,
        document_status=document.status,
    )
    return _to_read(document)


async def generate(
    session: AsyncSession,
    actor: Actor,
    raw_issue_key: str,
    *,
    regenerate: bool = False,
    settings: Settings | None = None,
    source: AuditSource = AuditSource.API,
) -> BrdRead:
    """Generate a BRD for one issue, or return the existing one.

    Args:
        session: Database session. Committed here when a row is written. The
            audit record uses its own session; see ``audit.service``.
        actor: The authenticated end user.
        raw_issue_key: The requested key, validated here.
        regenerate: Generate even if a document exists. Off by default because
            generating costs money and spends rate limit.
        settings: Overrides the process settings, for tests.
        source: Which surface the call arrived through.

    Raises:
        ValidationFailedError: The issue key is malformed.
        ForbiddenError: The actor is unknown or deactivated.
        NotFoundError: The issue does not exist or the actor has no grant for
            its project.
        ServiceUnavailableError: Generation is not configured on this server.
        UpstreamError, UpstreamTimeoutError, RateLimitedError: The model
            provider failed, or returned something unusable.
    """
    settings = settings or get_settings()
    issue_key = IssueKeyPath(issue_key=raw_issue_key).issue_key

    # Gates 1-4, delegated. Raises before anything here spends money.
    issue = await _gated_issue(
        session, actor, issue_key, action=GENERATE_ACTION, settings=settings, source=source
    )
    user = await jira_repository.get_user_by_email(session, actor.email)

    # Gate 5: is generation possible at all? Before the existing-document
    # check, so an unconfigured server says so rather than appearing to work
    # for issues that happen to have a document already.
    if not settings.generation_available:
        await _audit(
            actor,
            AuditOutcome.ERROR,
            action=GENERATE_ACTION,
            issue_key=issue_key,
            reason="generation_not_configured",
            user=user,
            source=source,
        )
        raise ServiceUnavailableError(
            "Document generation is not configured on this server. "
            "Ask an administrator to set LLM_MODEL and LLM_API_KEY."
        )

    # Gate 6: has this already been done?
    if not regenerate:
        existing = await repository.get_current_document(session, issue_key)
        if existing is not None:
            _logger.info("existing brd returned", extra={"issue_key": issue_key})
            await _audit(
                actor,
                AuditOutcome.ALLOWED,
                action=GENERATE_ACTION,
                issue_key=issue_key,
                user=user,
                source=source,
                reused_existing=True,
                document_status=existing.status,
            )
            return _to_read(existing, reused_existing=True)

    prompt = build_brd_prompt(issue, max_tokens=settings.llm_max_tokens)

    # The pending row is committed before the model is called, so a generation
    # that never returns leaves a visible row instead of nothing. The partial
    # index on pending rows exists to find them.
    document = await repository.open_attempt(
        session,
        issue_key=issue_key,
        prompt_version=prompt.version,
        actor_email=actor.email,
        actor_user_id=user.id if user is not None else None,
    )
    await session.commit()

    try:
        completion = await llm.generate(
            system=prompt.system,
            user=prompt.user,
            max_tokens=prompt.max_tokens,
            settings=settings,
        )
    except AppError as exc:
        await _fail(
            session,
            document,
            actor,
            issue_key=issue_key,
            user=user,
            source=source,
            reason=exc.code.value,
        )
        raise

    if completion.was_truncated:
        # A cut-off requirements document looks complete rather than short, so
        # it would pass validation. Nothing partial is stored.
        await _fail(
            session,
            document,
            actor,
            issue_key=issue_key,
            user=user,
            source=source,
            reason="truncated",
            completion=completion,
        )
        # UpstreamError, so it renders as a 502: the provider produced
        # something this platform cannot use, which is not the caller's fault
        # and not a bug in the request.
        raise UpstreamError(
            "The model's reply was cut off before it finished. "
            "Try again, or ask an administrator to raise LLM_MAX_TOKENS."
        )

    report = validate(completion.text)
    if not report.ok or report.document is None:
        _logger.warning(
            "generated document failed validation",
            extra={"issue_key": issue_key, "error_count": len(report.errors)},
        )
        await _fail(
            session,
            document,
            actor,
            issue_key=issue_key,
            user=user,
            source=source,
            reason="invalid_format",
            completion=completion,
            error_count=len(report.errors),
        )
        raise UpstreamError("The model did not return a usable requirements document. Try again.")

    parsed = report.document
    status = BrdStatus.NEEDS_INFO if parsed.needs_more_information else BrdStatus.READY
    await repository.complete_attempt(
        session,
        document,
        markdown=parsed.markdown,
        status=status,
        requirement_count=len(parsed.requirements),
        scenario_count=parsed.scenario_count,
        open_question_count=len(parsed.open_questions),
        model_reported=completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
    )
    await session.commit()

    _logger.info(
        "brd generated",
        extra={
            "issue_key": issue_key,
            "document_status": status.value,
            "requirement_count": len(parsed.requirements),
        },
    )
    await _audit(
        actor,
        AuditOutcome.ALLOWED,
        action=GENERATE_ACTION,
        issue_key=issue_key,
        user=user,
        source=source,
        document_status=status.value,
        requirement_count=len(parsed.requirements),
        scenario_count=parsed.scenario_count,
        prompt_version=prompt.version,
        model_reported=completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
    )
    return _to_read(document)


async def _fail(
    session: AsyncSession,
    document: BrdDocument,
    actor: Actor,
    *,
    issue_key: str,
    user: User | None,
    source: AuditSource,
    reason: str,
    completion: llm.Completion | None = None,
    **detail: Any,
) -> None:
    """Mark the attempt failed, commit it, and audit it.

    Committed before the exception propagates, for the same reason the audit
    service opens its own session (ADR-013): the record of a failure must
    survive the failure that caused it.
    """
    await repository.fail_attempt(
        session,
        document,
        reason=reason,
        model_reported=completion.model if completion else None,
        prompt_tokens=completion.prompt_tokens if completion else None,
        completion_tokens=completion.completion_tokens if completion else None,
    )
    await session.commit()
    await _audit(
        actor,
        AuditOutcome.ERROR,
        action=GENERATE_ACTION,
        issue_key=issue_key,
        reason=reason,
        user=user,
        source=source,
        **detail,
    )

"""Business logic for reading Jira issues.

The order of operations in :func:`get_issue` is the security property of this
module. Read it as a sequence of gates, each of which must pass before the next
is attempted:

1. Is the issue key well-formed?
2. Is the caller a known, active user?
3. Does that user hold a grant for this issue's project?
4. Only then: is there a fresh cached copy, or do we call Jira?

Step 3 before step 4 is the point. The Jira service account can see far more
than any individual user should, so calling Jira first and filtering afterwards
would leak existence through timing, and one forgotten branch would leak the
issue itself.

**Every outcome is audited**, allowed and denied alike, through
:func:`app.modules.audit.service.record`, which writes in its own transaction
so that a denial survives the exception that reports it. The audit row is
written before the result is returned or the error raised, so there is no path
through this function that serves data without a record of having done so.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ForbiddenError, NotFoundError
from app.core.security import Actor
from app.models.audit_log import AuditOutcome, AuditSource
from app.models.jira_issue import JiraIssue
from app.models.user import User
from app.modules.audit.schemas import AuditEvent
from app.modules.audit.service import record as record_audit
from app.modules.jira import adf, client, repository
from app.modules.jira.schemas import IssueKeyPath, IssueRead

_logger = logging.getLogger("app.jira.service")


def project_key_of(issue_key: str) -> str:
    """Return the project key embedded in an issue key.

    ``ABC-123`` yields ``ABC``. Must stay consistent with the
    ``split_part(issue_key, '-', 1)`` generated column on ``jira_issues``,
    because this value is what the permission check compares.
    """
    return issue_key.split("-", 1)[0]


#: Recorded as the ``action`` for every read attempt, successful or not.
READ_ACTION = "jira_issue.read"
_RESOURCE_TYPE = "jira_issue"


async def _audit(
    actor: Actor,
    outcome: AuditOutcome,
    *,
    issue_key: str | None,
    reason: str | None = None,
    user: User | None = None,
    source: AuditSource = AuditSource.API,
    **detail: str | int | float | bool | None,
) -> None:
    """Record one read attempt.

    Kept local to this module so every exit path from :func:`get_issue` reads
    as one line, which is what makes it possible to check at a glance that
    none of them is missing.
    """
    payload: dict[str, str | int | float | bool | None] = dict(detail)
    if reason is not None:
        payload["reason"] = reason
    await record_audit(
        AuditEvent(
            actor_email=actor.email,
            action=READ_ACTION,
            outcome=outcome,
            source=source,
            resource_type=_RESOURCE_TYPE,
            resource_id=issue_key,
            detail=payload,
        ),
        actor_user_id=user.id if user is not None else None,
    )


async def get_issue(
    session: AsyncSession,
    actor: Actor,
    raw_issue_key: str,
    *,
    force_refresh: bool = False,
    settings: Settings | None = None,
    source: AuditSource = AuditSource.API,
) -> IssueRead:
    """Return one issue for this actor, from cache when it is fresh.

    Args:
        session: Database session. This function commits it when it writes.
            The audit record does **not** use it; see the module docstring.
        actor: The authenticated end user, resolved from the actor header.
        raw_issue_key: The requested key, validated here.
        force_refresh: Skip the cache and call Jira. Nothing exposes this over
            HTTP yet; it exists so a later refresh job does not have to
            reimplement the gates above.
        settings: Overrides the process settings, for tests.
        source: Which surface this call arrived through, recorded in the audit
            row. Step 7's MCP server passes ``AuditSource.MCP``.

    Raises:
        ValidationFailedError: The issue key is malformed.
        ForbiddenError: The actor is not a registered user, or is deactivated.
        NotFoundError: The issue does not exist, **or** the actor has no grant
            for its project. The two are deliberately indistinguishable; see
            ADR-010.
        UpstreamError, UpstreamTimeoutError, RateLimitedError: Jira failed.
    """
    settings = settings or get_settings()

    # Gate 1: shape. Pydantic raises on a malformed key, which the exception
    # handler renders as a 422.
    #
    # Not audited, deliberately: a malformed key never reaches a user record,
    # the cache or Jira, so there is no access decision to record. Over HTTP
    # the router rejects it before this line is even reached.
    issue_key = IssueKeyPath(issue_key=raw_issue_key).issue_key
    project_key = project_key_of(issue_key)

    # Gate 2: is this a user we know?
    user = await repository.get_user_by_email(session, actor.email)
    if user is None:
        _logger.warning("unknown actor", extra={"actor": actor.email})
        await _audit(
            actor,
            AuditOutcome.DENIED,
            issue_key=issue_key,
            reason="actor_not_registered",
            source=source,
        )
        raise ForbiddenError(
            "This account is not registered on the QA platform. Ask an administrator to add it."
        )
    if not user.is_active:
        _logger.warning("inactive actor", extra={"actor": actor.email})
        await _audit(
            actor,
            AuditOutcome.DENIED,
            issue_key=issue_key,
            reason="actor_inactive",
            user=user,
            source=source,
        )
        raise ForbiddenError("This account is deactivated.")

    # Gate 3: may this user see this project at all? Before any Jira call.
    if not await repository.has_project_access(session, user.id, project_key):
        _logger.warning(
            "project access denied",
            extra={"actor": actor.email, "project_key": project_key, "issue_key": issue_key},
        )
        await _audit(
            actor,
            AuditOutcome.DENIED,
            issue_key=issue_key,
            reason="no_project_access",
            user=user,
            source=source,
            project_key=project_key,
        )
        # Same error as a genuinely missing issue, so a caller cannot use this
        # endpoint to discover which projects or issues exist. The audit row
        # above carries the real reason, which is how an administrator answers
        # "why can't I see this?".
        raise NotFoundError(f"Issue {issue_key} was not found.")

    # Gate 4: cache, then Jira.
    cached = await repository.get_cached_issue(session, issue_key)
    if not force_refresh and cached is not None and _is_fresh(cached, settings):
        _logger.info("issue served from cache", extra={"issue_key": issue_key})
        await _audit(
            actor,
            AuditOutcome.ALLOWED,
            issue_key=issue_key,
            user=user,
            source=source,
            cache="hit",
        )
        return _to_read(cached, from_cache=True)

    try:
        payload = await client.fetch_issue(issue_key, settings)
    except AppError as exc:
        # Includes Jira's 404, which covers both "no such issue" and "the
        # service account cannot see it". Recorded as an error rather than a
        # denial: the platform's own authorisation already passed, so this is
        # an upstream outcome, not an access decision.
        await _audit(
            actor,
            AuditOutcome.ERROR,
            issue_key=issue_key,
            reason=exc.code.value,
            user=user,
            source=source,
        )
        raise

    parsed = _parse_issue(payload, issue_key, settings)
    row = await repository.upsert_issue(session, **parsed)
    await session.commit()
    _logger.info(
        "issue fetched from jira",
        extra={"issue_key": issue_key, "refreshed": cached is not None},
    )
    await _audit(
        actor,
        AuditOutcome.ALLOWED,
        issue_key=issue_key,
        user=user,
        source=source,
        cache="miss",
        refreshed=cached is not None,
    )
    return _to_read(row, from_cache=False)


def _is_fresh(issue: JiraIssue, settings: Settings) -> bool:
    """True if the cached row is within the configured TTL.

    A TTL of 0 disables the cache, which is the natural reading of
    ``JIRA_CACHE_TTL_SECONDS=0`` and useful while debugging.
    """
    if settings.jira_cache_ttl_seconds == 0:
        return False
    age_limit = datetime.now(UTC) - timedelta(seconds=settings.jira_cache_ttl_seconds)
    return issue.fetched_at > age_limit


def _parse_issue(
    payload: dict[str, object], issue_key: str, settings: Settings
) -> dict[str, object]:
    """Turn Jira's JSON into the columns ``upsert_issue`` expects.

    Defensive about shape throughout. Every field is read with ``.get`` and
    type-checked, because this payload comes from outside the system and a
    missing or differently-typed field must produce a usable issue rather than
    a 500.
    """
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = {}

    summary = fields.get("summary")
    summary_text = summary.strip() if isinstance(summary, str) and summary.strip() else issue_key

    description_raw = fields.get("description")
    description = adf.to_text(description_raw)

    return {
        "issue_key": issue_key,
        "summary": summary_text,
        "description": description,
        "acceptance_criteria": _acceptance_criteria(fields, description_raw, settings),
        "issue_type": _nested_name(fields, "issuetype"),
        "status": _nested_name(fields, "status"),
        "jira_updated_at": _parse_timestamp(fields.get("updated")),
        "fetched_at": datetime.now(UTC),
    }


def _acceptance_criteria(
    fields: dict[str, object], description_raw: object, settings: Settings
) -> str | None:
    """Resolve acceptance criteria from the custom field, else the description.

    The configured custom field wins when it holds anything. Jira returns a
    rich-text custom field as ADF and a plain-text one as a string, so
    ``adf.to_text`` handles both.

    The fallback parses an "Acceptance Criteria" heading out of the
    description. That keeps the endpoint useful on a tenant where nobody has
    found the custom field id yet, which is the common starting state.
    """
    field_id = settings.jira_acceptance_criteria_field
    if field_id:
        from_field = adf.to_text(fields.get(field_id))
        if from_field:
            return from_field
    return adf.extract_acceptance_criteria(description_raw)


def _nested_name(fields: dict[str, object], key: str) -> str | None:
    """Read ``fields[key]["name"]``, tolerating any other shape."""
    value = fields.get(key)
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _parse_timestamp(value: object) -> datetime | None:
    """Parse Jira's ISO-8601 timestamp, or return None.

    Jira sends offsets without a colon, for example ``+0530``, which
    ``datetime.fromisoformat`` rejected before Python 3.11. It accepts them
    now, but the guard stays: an unparseable timestamp must not fail the whole
    request over a field nothing critical depends on.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        _logger.info("could not parse jira timestamp")
        return None


def _to_read(issue: JiraIssue, *, from_cache: bool) -> IssueRead:
    """Project a cached row onto the API response model."""
    return IssueRead(
        issue_key=issue.issue_key,
        project_key=issue.project_key,
        summary=issue.summary,
        description=issue.description,
        acceptance_criteria=issue.acceptance_criteria,
        issue_type=issue.issue_type,
        status=issue.status,
        jira_updated_at=issue.jira_updated_at,
        fetched_at=issue.fetched_at,
        from_cache=from_cache,
    )

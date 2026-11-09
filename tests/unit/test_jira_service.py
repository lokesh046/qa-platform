"""Tests for ``app.modules.jira.service``: the four gates, and the audit trail.

The module docstring of the code under test calls the order of operations its
security property. That is what most of this file checks, and it is checked by
observation rather than by reading: the stub repository **records the order it
was called in**, so "no Jira call before project access passed" is an
assertion, not a code review.

No database. The four repository functions are replaced with stubs, and the
audit writer is replaced with a list. That is the only way these tests can be
committed: the real audit writer opens its own session (ADR-013), so a test
touching it would need Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import ForbiddenError, NotFoundError, RateLimitedError, UpstreamError
from app.core.security import Actor
from app.models.audit_log import AuditOutcome, AuditSource
from app.models.jira_issue import JiraIssue
from app.models.user import User, UserRole
from app.modules.audit.schemas import AuditEvent
from app.modules.jira import repository, service
from tests.conftest import TEST_ACTOR, TEST_JIRA_BASE_URL, make_settings

ISSUE_PATH = "/rest/api/3/issue/ABC-123"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    """Stands in for ``AsyncSession``. The service only ever commits it."""

    commits: int = 0

    async def commit(self) -> None:
        """Count a commit."""
        self.commits += 1


@dataclass
class FakeRepository:
    """Records every repository call, in order.

    The ``calls`` list is the point of this class. Half the assertions in this
    file are about what did *not* happen yet when something else happened.
    """

    user: User | None = None
    has_access: bool = True
    cached: JiraIssue | None = None
    calls: list[str] = field(default_factory=list)
    upserted: dict[str, Any] = field(default_factory=dict)

    async def get_user_by_email(self, _session: object, email: str) -> User | None:
        """Look up the stub user, honouring the real function's lowercasing."""
        self.calls.append("get_user_by_email")
        if self.user is not None and self.user.email == email.lower():
            return self.user
        return None

    async def has_project_access(
        self, _session: object, _user_id: uuid.UUID, project_key: str
    ) -> bool:
        """Report the configured answer, recording the project asked about."""
        self.calls.append(f"has_project_access:{project_key}")
        return self.has_access

    async def get_cached_issue(self, _session: object, _issue_key: str) -> JiraIssue | None:
        """Return the configured cached row, if any."""
        self.calls.append("get_cached_issue")
        return self.cached

    async def upsert_issue(self, _session: object, **values: Any) -> JiraIssue:
        """Record the written columns and return a row shaped like the real one."""
        self.calls.append("upsert_issue")
        self.upserted = values
        return JiraIssue(project_key=service.project_key_of(values["issue_key"]), **values)


def make_user(*, email: str = TEST_ACTOR, is_active: bool = True) -> User:
    """A detached ``User`` row. Never flushed, so no database is involved."""
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name="QA One",
        role=UserRole.QA_ENGINEER,
        is_active=is_active,
    )


def cached_row(*, age_seconds: int = 0, issue_key: str = "ABC-123") -> JiraIssue:
    """A cached issue row, optionally stale."""
    return JiraIssue(
        issue_key=issue_key,
        project_key=issue_key.split("-", 1)[0],
        summary="Cached summary",
        description="Cached description",
        acceptance_criteria="- cached criterion",
        issue_type="Bug",
        status="To Do",
        jira_updated_at=datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
        fetched_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


def jira_payload(**field_overrides: Any) -> dict[str, Any]:
    """A Jira issue payload, in the shape the v3 API returns."""
    fields: dict[str, Any] = {
        "summary": "Login rejects a valid password",
        "issuetype": {"name": "Bug"},
        "status": {"name": "To Do"},
        "updated": "2026-11-01T09:30:00.000+0530",
        "description": None,
    }
    fields.update(field_overrides)
    return {"key": "ABC-123", "fields": fields}


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepository:
    """Replace the module's repository functions with the recording stub."""
    fake = FakeRepository(user=make_user())
    for name in ("get_user_by_email", "has_project_access", "get_cached_issue", "upsert_issue"):
        monkeypatch.setattr(repository, name, getattr(fake, name))
    return fake


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[AuditEvent, uuid.UUID | None]]:
    """Capture audit events instead of writing them.

    The real ``record`` opens its own session and commits it, which is exactly
    what makes a denial survive the exception that reports it — and exactly
    what cannot run without a database.
    """
    recorded: list[tuple[AuditEvent, uuid.UUID | None]] = []

    async def fake_record(event: AuditEvent, *, actor_user_id: uuid.UUID | None = None) -> None:
        recorded.append((event, actor_user_id))

    monkeypatch.setattr(service, "record_audit", fake_record)
    return recorded


@pytest.fixture
def jira_mock() -> Iterator[respx.MockRouter]:
    """Intercept every request to the Jira host."""
    with respx.mock(base_url=TEST_JIRA_BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def _jira_client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Build the Jira client from the test settings, and close it afterwards."""
    from app.modules.jira import client as jira_client

    monkeypatch.setattr("app.modules.jira.client.get_settings", lambda: settings)
    jira_client.reset_http_client_cache()
    try:
        yield
    finally:
        await jira_client.close_http_client()
        jira_client.reset_http_client_cache()


pytestmark = pytest.mark.usefixtures("_jira_client")

ACTOR = Actor(email=TEST_ACTOR)


def outcomes(recorded: list[tuple[AuditEvent, uuid.UUID | None]]) -> list[AuditOutcome]:
    """The outcome of each captured audit event."""
    return [event.outcome for event, _ in recorded]


def reasons(recorded: list[tuple[AuditEvent, uuid.UUID | None]]) -> list[str | None]:
    """The ``reason`` detail of each captured audit event."""
    return [event.detail.get("reason") for event, _ in recorded]


# ---------------------------------------------------------------------------
# Gate 1: the issue key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["abc-123", "ABC", "", "ABC-1;DROP TABLE users"])
async def test_a_malformed_key_is_rejected_before_anything_happens(
    repo: FakeRepository, audit: list[Any], settings: Settings, key: str
):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await service.get_issue(FakeSession(), ACTOR, key, settings=settings)
    # No user lookup, no access check, no Jira call, and no audit row: a
    # malformed key reaches no access decision, so there is nothing to record.
    assert repo.calls == []
    assert audit == []


# ---------------------------------------------------------------------------
# Gate 2: identity
# ---------------------------------------------------------------------------


async def test_an_unknown_actor_is_refused_and_audited(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    repo.user = None
    with pytest.raises(ForbiddenError) as excinfo:
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert "not registered" in str(excinfo.value)
    assert outcomes(audit) == [AuditOutcome.DENIED]
    assert reasons(audit) == ["actor_not_registered"]
    # The access check never ran, so no Jira call could have been made.
    assert repo.calls == ["get_user_by_email"]


async def test_an_unknown_actor_row_has_no_user_id(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    # actor_email is denormalised precisely so a row survives without one.
    repo.user = None
    with pytest.raises(ForbiddenError):
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    event, actor_user_id = audit[0]
    assert actor_user_id is None
    assert event.actor_email == TEST_ACTOR


async def test_a_deactivated_actor_is_refused_and_audited(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    repo.user = make_user(is_active=False)
    with pytest.raises(ForbiddenError) as excinfo:
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert "deactivated" in str(excinfo.value)
    assert reasons(audit) == ["actor_inactive"]
    assert repo.calls == ["get_user_by_email"]


async def test_a_deactivated_actor_row_carries_the_user_id(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    user = make_user(is_active=False)
    repo.user = user
    with pytest.raises(ForbiddenError):
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert audit[0][1] == user.id


# ---------------------------------------------------------------------------
# Gate 3: project access, before Jira
# ---------------------------------------------------------------------------


async def test_no_project_grant_looks_exactly_like_a_missing_issue(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    # ADR-010: without this, any holder of the service token could enumerate
    # which projects and issues exist without holding a single grant.
    repo.has_access = False
    with pytest.raises(NotFoundError) as excinfo:
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert str(excinfo.value) == "Issue ABC-123 was not found."


async def test_a_denial_records_the_real_reason(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    # The error is ambiguous on purpose; the audit row is not, which is how an
    # administrator answers "why can't I see this?".
    repo.has_access = False
    with pytest.raises(NotFoundError):
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    event, _ = audit[0]
    assert event.outcome is AuditOutcome.DENIED
    assert event.detail["reason"] == "no_project_access"
    assert event.detail["project_key"] == "ABC"
    assert event.resource_id == "ABC-123"


async def test_no_jira_call_is_made_without_project_access(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # The load-bearing assertion of this file. The Jira service account can
    # see far more than any individual user should.
    route = jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    repo.has_access = False
    with pytest.raises(NotFoundError):
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert route.call_count == 0
    # The cache was not even consulted.
    assert repo.calls == ["get_user_by_email", "has_project_access:ABC"]


async def test_the_access_check_uses_the_project_from_the_key(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    repo.has_access = False
    with pytest.raises(NotFoundError):
        await service.get_issue(FakeSession(), ACTOR, "DEF-9", settings=settings)
    assert "has_project_access:DEF" in repo.calls


def test_project_key_derivation_matches_the_generated_column():
    # The database derives project_key with split_part(issue_key, '-', 1).
    # This function must agree, because its value is what the check compares.
    assert service.project_key_of("ABC-123") == "ABC"
    assert service.project_key_of("PROJECT2024-1") == "PROJECT2024"


# ---------------------------------------------------------------------------
# Gate 4: cache, then Jira
# ---------------------------------------------------------------------------


async def test_a_fresh_cached_issue_is_served_without_calling_jira(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    route = jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    repo.cached = cached_row(age_seconds=10)
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.from_cache is True
    assert issue.summary == "Cached summary"
    assert route.call_count == 0
    assert audit[0][0].detail["cache"] == "hit"


async def test_a_stale_cached_issue_is_refreshed(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    route = jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    repo.cached = cached_row(age_seconds=settings.jira_cache_ttl_seconds + 60)
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert route.call_count == 1
    assert issue.from_cache is False
    assert issue.summary == "Login rejects a valid password"
    assert audit[0][0].detail == {"cache": "miss", "refreshed": True}


async def test_a_zero_ttl_disables_the_cache(
    repo: FakeRepository, audit: list[Any], jira_mock: respx.MockRouter
):
    # The natural reading of JIRA_CACHE_TTL_SECONDS=0, and useful when
    # debugging why an issue looks out of date.
    route = jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    repo.cached = cached_row(age_seconds=0)
    issue = await service.get_issue(
        FakeSession(), ACTOR, "ABC-123", settings=make_settings(jira_cache_ttl_seconds=0)
    )
    assert route.call_count == 1
    assert issue.from_cache is False


async def test_force_refresh_skips_a_fresh_cache(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    route = jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    repo.cached = cached_row(age_seconds=1)
    await service.get_issue(FakeSession(), ACTOR, "ABC-123", force_refresh=True, settings=settings)
    assert route.call_count == 1


async def test_force_refresh_does_not_skip_the_access_gates(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # force_refresh is exposed nowhere yet; it exists so a later refresh job
    # does not reimplement the gates. It must not become a way around them.
    route = jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    repo.has_access = False
    with pytest.raises(NotFoundError):
        await service.get_issue(
            FakeSession(), ACTOR, "ABC-123", force_refresh=True, settings=settings
        )
    assert route.call_count == 0


async def test_a_cache_miss_is_fetched_cached_and_committed(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    session = FakeSession()
    issue = await service.get_issue(session, ACTOR, "ABC-123", settings=settings)
    assert repo.calls[-1] == "upsert_issue"
    assert session.commits == 1
    assert issue.from_cache is False
    assert audit[0][0].detail == {"cache": "miss", "refreshed": False}


async def test_the_cached_row_never_holds_the_raw_payload(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # ADR-009: the cache stores a projection, not Jira's response. A raw
    # payload would quietly accumulate reporters, comments and attachments.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert set(repo.upserted) == {
        "issue_key",
        "summary",
        "description",
        "acceptance_criteria",
        "issue_type",
        "status",
        "jira_updated_at",
        "fetched_at",
    }


# ---------------------------------------------------------------------------
# Parsing Jira's payload
# ---------------------------------------------------------------------------


async def test_fields_are_projected_onto_the_response(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.issue_key == "ABC-123"
    assert issue.project_key == "ABC"
    assert issue.issue_type == "Bug"
    assert issue.status == "To Do"


async def test_jiras_offset_timestamp_is_normalised_to_utc(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # Jira sends +0530 without a colon. 09:30+0530 is 04:00Z, and asserting
    # the wall clock instead of the instant is an easy mistake to make.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.jira_updated_at is not None
    assert issue.jira_updated_at.astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", ["", "   ", "not a date", None, 17, {"a": 1}])
async def test_an_unparseable_timestamp_does_not_fail_the_request(
    repo: FakeRepository,
    audit: list[Any],
    settings: Settings,
    jira_mock: respx.MockRouter,
    value: object,
):
    # Nothing critical depends on this field, so it must not cost the issue.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload(updated=value))
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.jira_updated_at is None


@pytest.mark.parametrize("summary", [None, "", "   ", 42, {"text": "x"}])
async def test_a_missing_summary_falls_back_to_the_issue_key(
    repo: FakeRepository,
    audit: list[Any],
    settings: Settings,
    jira_mock: respx.MockRouter,
    summary: object,
):
    # summary is NOT NULL in the cache, and the response model requires it.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload(summary=summary))
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.summary == "ABC-123"


async def test_a_payload_with_no_fields_object_still_produces_an_issue(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(json={"key": "ABC-123"})
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.summary == "ABC-123"
    assert issue.description is None
    assert issue.issue_type is None


@pytest.mark.parametrize("value", [{"no_name": 1}, {"name": ""}, "Bug", None, []])
async def test_a_malformed_nested_name_becomes_none(
    repo: FakeRepository,
    audit: list[Any],
    settings: Settings,
    jira_mock: respx.MockRouter,
    value: object,
):
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload(issuetype=value, status=value))
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.issue_type is None
    assert issue.status is None


# ---------------------------------------------------------------------------
# Acceptance criteria: the custom field, then the description
# ---------------------------------------------------------------------------

AC_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "A user cannot log in."}]},
        {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "Acceptance Criteria"}],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Valid password grants access"}],
                        }
                    ],
                }
            ],
        },
    ],
}


async def test_the_custom_field_wins_when_it_holds_anything(
    repo: FakeRepository, audit: list[Any], jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(
        json=jira_payload(description=AC_DESCRIPTION, customfield_10040="- from the field")
    )
    issue = await service.get_issue(
        FakeSession(),
        ACTOR,
        "ABC-123",
        settings=make_settings(jira_acceptance_criteria_field="customfield_10040"),
    )
    assert issue.acceptance_criteria == "- from the field"


@pytest.mark.parametrize("empty", [None, "", "   ", {"type": "doc", "content": []}])
async def test_an_empty_custom_field_falls_back_to_the_description(
    repo: FakeRepository, audit: list[Any], jira_mock: respx.MockRouter, empty: object
):
    # The common starting state: the field exists on the tenant but nobody has
    # filled it in on this issue.
    jira_mock.get(ISSUE_PATH).respond(
        json=jira_payload(description=AC_DESCRIPTION, customfield_10040=empty)
    )
    issue = await service.get_issue(
        FakeSession(),
        ACTOR,
        "ABC-123",
        settings=make_settings(jira_acceptance_criteria_field="customfield_10040"),
    )
    assert issue.acceptance_criteria == "- Valid password grants access"


async def test_with_no_field_configured_the_description_is_parsed(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # Keeps the endpoint useful on a tenant where nobody has found the custom
    # field id yet; see ADR-011.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload(description=AC_DESCRIPTION))
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.acceptance_criteria == "- Valid password grants access"
    assert issue.description == (
        "A user cannot log in.\n\nAcceptance Criteria\n\n- Valid password grants access"
    )


async def test_no_criteria_anywhere_is_null_not_empty(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload(description="Just a sentence."))
    issue = await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert issue.acceptance_criteria is None


# ---------------------------------------------------------------------------
# Upstream failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "reason"),
    [
        (404, NotFoundError, "not_found"),
        (401, UpstreamError, "upstream_error"),
        (429, RateLimitedError, "rate_limited"),
        (500, UpstreamError, "upstream_error"),
    ],
)
async def test_an_upstream_failure_is_audited_as_an_error_and_re_raised(
    repo: FakeRepository,
    audit: list[Any],
    settings: Settings,
    jira_mock: respx.MockRouter,
    status: int,
    expected: type[Exception],
    reason: str,
):
    # Recorded as an error, not a denial: the platform's own authorisation
    # already passed, so this is an upstream outcome, not an access decision.
    jira_mock.get(ISSUE_PATH).respond(status, json={})
    with pytest.raises(expected):
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert outcomes(audit) == [AuditOutcome.ERROR]
    assert reasons(audit) == [reason]


async def test_nothing_is_cached_when_jira_fails(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(500, json={})
    session = FakeSession()
    with pytest.raises(UpstreamError):
        await service.get_issue(session, ACTOR, "ABC-123", settings=settings)
    assert "upsert_issue" not in repo.calls
    assert session.commits == 0


async def test_a_jira_timeout_is_audited(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(Exception, match="did not respond"):
        await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert reasons(audit) == ["upstream_timeout"]


# ---------------------------------------------------------------------------
# The audit trail as a whole
# ---------------------------------------------------------------------------


async def test_every_outcome_writes_exactly_one_row(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # There is no path through get_issue that serves data without a record of
    # having done so, and none that writes two.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert len(audit) == 1
    assert outcomes(audit) == [AuditOutcome.ALLOWED]


async def test_the_audit_action_and_resource_type_are_fixed(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    event, _ = audit[0]
    assert event.action == "jira_issue.read"
    assert event.resource_type == "jira_issue"
    assert event.resource_id == "ABC-123"


async def test_the_source_defaults_to_api_and_can_be_set_to_mcp(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # Anyone counting reads must filter on source or they double-count, since
    # the MCP server writes its own row for the same call.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    assert audit[0][0].source is AuditSource.API
    audit.clear()
    repo.cached = cached_row(age_seconds=1)
    await service.get_issue(
        FakeSession(), ACTOR, "ABC-123", settings=settings, source=AuditSource.MCP
    )
    assert audit[0][0].source is AuditSource.MCP


async def test_the_audit_detail_holds_only_scalars(
    repo: FakeRepository, audit: list[Any], settings: Settings, jira_mock: respx.MockRouter
):
    # AuditEvent enforces this, so this test is really about the service not
    # trying to pass something richer.
    jira_mock.get(ISSUE_PATH).respond(json=jira_payload())
    await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
    for value in audit[0][0].detail.values():
        assert isinstance(value, str | int | float | bool | None)


async def test_the_audited_actor_is_canonicalised(
    repo: FakeRepository, audit: list[Any], settings: Settings
):
    # One person must be one actor in the trail. EmailStr lowercases the domain
    # but not the local part, so without canonicalisation the same user could
    # appear as several actors and a run of denials would be split across them.
    # See ADR-017.
    repo.has_access = False
    with pytest.raises(NotFoundError):
        await service.get_issue(
            FakeSession(), Actor(email="QA.One@Acme-QA.co"), "ABC-123", settings=settings
        )
    assert audit[0][0].actor_email == TEST_ACTOR

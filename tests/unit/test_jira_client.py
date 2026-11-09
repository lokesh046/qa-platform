"""Tests for ``app.modules.jira.client``: the HTTP gateway to Jira.

Jira is mocked with ``respx``, so nothing here resolves a hostname or opens a
socket. Every branch of ``_handle_response`` is covered, because each one maps
an upstream status onto an error the API contract promises (docs/api.md), and
because a wrong mapping is invisible until the day Jira returns that status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import (
    ErrorCode,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.modules.jira import client as jira_client
from tests.conftest import TEST_JIRA_BASE_URL, make_settings

ISSUE_PATH = "/rest/api/3/issue/ABC-123"


def issue_payload(key: str = "ABC-123") -> dict[str, Any]:
    """A minimal Jira issue, in the shape the v3 API returns."""
    return {
        "key": key,
        "fields": {
            "summary": "Login rejects a valid password",
            "issuetype": {"name": "Bug"},
            "status": {"name": "To Do"},
            "updated": "2026-11-01T09:30:00.000+0530",
            "description": None,
        },
    }


@pytest.fixture
def jira_mock() -> Iterator[respx.MockRouter]:
    """Intercept every request to the Jira host."""
    with respx.mock(base_url=TEST_JIRA_BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def _pooled_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Point the module's cached client at the test settings, then clean up.

    ``get_http_client`` is an ``lru_cache`` over the process settings, so a
    test that did not reset it would either reuse another test's client or
    build one from the developer's ``.env``.
    """
    monkeypatch.setattr("app.modules.jira.client.get_settings", lambda: settings)
    jira_client.reset_http_client_cache()
    try:
        yield
    finally:
        await jira_client.close_http_client()
        jira_client.reset_http_client_cache()


pytestmark = pytest.mark.usefixtures("_pooled_client")


# ---------------------------------------------------------------------------
# The client itself
# ---------------------------------------------------------------------------


async def test_the_client_is_built_once_and_reused():
    # One connection pool for the process. Building one per call throws away
    # connection reuse and the TLS handshake with it.
    assert jira_client.get_http_client() is jira_client.get_http_client()


async def test_the_client_does_not_follow_redirects():
    # Jira answers an unauthenticated request with a redirect to its login
    # page. Following it turns a 401 into a confusing 200 of HTML.
    assert jira_client.get_http_client().follow_redirects is False


async def test_the_client_sends_basic_auth(jira_mock: respx.MockRouter):
    route = jira_mock.get(ISSUE_PATH).respond(json=issue_payload())
    await jira_client.fetch_issue("ABC-123")
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")


async def test_the_token_is_not_in_the_url_or_the_query(jira_mock: respx.MockRouter):
    # A credential in a URL ends up in access logs and proxy logs.
    route = jira_mock.get(ISSUE_PATH).respond(json=issue_payload())
    await jira_client.fetch_issue("ABC-123")
    assert "jira-api-token-for-tests" not in str(route.calls[0].request.url)


async def test_only_the_stored_fields_are_requested(jira_mock: respx.MockRouter):
    # Not requesting comments, attachments and worklogs is the cheapest way to
    # guarantee they are never stored; see ADR-009.
    route = jira_mock.get(ISSUE_PATH).respond(json=issue_payload())
    await jira_client.fetch_issue("ABC-123")
    requested = route.calls[0].request.url.params["fields"].split(",")
    assert requested == ["summary", "description", "issuetype", "status", "updated"]


async def test_the_acceptance_criteria_field_is_requested_when_configured(
    jira_mock: respx.MockRouter,
):
    route = jira_mock.get(ISSUE_PATH).respond(json=issue_payload())
    await jira_client.fetch_issue(
        "ABC-123", make_settings(jira_acceptance_criteria_field="customfield_10040")
    )
    assert "customfield_10040" in route.calls[0].request.url.params["fields"]


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


async def test_a_200_returns_the_parsed_payload(jira_mock: respx.MockRouter):
    jira_mock.get(ISSUE_PATH).respond(json=issue_payload())
    payload = await jira_client.fetch_issue("ABC-123")
    assert payload["fields"]["summary"] == "Login rejects a valid password"


async def test_a_404_becomes_not_found(jira_mock: respx.MockRouter):
    # Jira returns 404 rather than 403 for an issue the service account cannot
    # see, so the two are indistinguishable from here. "Not found" is both the
    # only option and the right answer for the API.
    jira_mock.get(ISSUE_PATH).respond(404, json={"errorMessages": ["Issue does not exist"]})
    with pytest.raises(NotFoundError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert excinfo.value.code is ErrorCode.NOT_FOUND
    assert "ABC-123" in str(excinfo.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_become_an_upstream_error(
    jira_mock: respx.MockRouter, status: int
):
    # Our misconfiguration, never the end user's problem, so the message stays
    # generic and the detail goes to the log.
    jira_mock.get(ISSUE_PATH).respond(status, json={"errorMessages": ["nope"]})
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert excinfo.value.code is ErrorCode.UPSTREAM_ERROR
    assert "credentials" in str(excinfo.value)


async def test_rejected_credentials_are_logged_at_error_with_what_to_check(
    jira_mock: respx.MockRouter, log_buffer: Any
):
    jira_mock.get(ISSUE_PATH).respond(401, json={})
    with pytest.raises(UpstreamError):
        await jira_client.fetch_issue("ABC-123")
    logged = log_buffer.getvalue()
    assert "ERROR" in logged
    assert "JIRA_SERVICE_EMAIL" in logged


async def test_a_429_becomes_rate_limited_and_passes_retry_after(jira_mock: respx.MockRouter):
    jira_mock.get(ISSUE_PATH).respond(429, headers={"Retry-After": "30"}, json={})
    with pytest.raises(RateLimitedError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert excinfo.value.details == {"retry_after": "30"}


async def test_a_429_without_retry_after_has_no_details(jira_mock: respx.MockRouter):
    jira_mock.get(ISSUE_PATH).respond(429, json={})
    with pytest.raises(RateLimitedError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert excinfo.value.details is None


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_jira_server_error_becomes_an_upstream_error(
    jira_mock: respx.MockRouter, status: int
):
    jira_mock.get(ISSUE_PATH).respond(status, json={})
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert "internal error" in str(excinfo.value)


@pytest.mark.parametrize("status", [201, 204, 302, 418])
async def test_any_other_status_becomes_an_upstream_error(jira_mock: respx.MockRouter, status: int):
    # Including the redirect, which is what an unauthenticated Jira returns.
    jira_mock.get(ISSUE_PATH).respond(status, json={})
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert "unexpected" in str(excinfo.value)


async def test_a_timeout_becomes_an_upstream_timeout(jira_mock: respx.MockRouter):
    jira_mock.get(ISSUE_PATH).mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(UpstreamTimeoutError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert excinfo.value.code is ErrorCode.UPSTREAM_TIMEOUT


async def test_a_connection_failure_becomes_an_upstream_error(jira_mock: respx.MockRouter):
    jira_mock.get(ISSUE_PATH).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert "reach Jira" in str(excinfo.value)


async def test_an_error_message_never_contains_the_issue_url(jira_mock: respx.MockRouter):
    # The issue key belongs in the audit trail, not in an error string that
    # may be rendered somewhere unexpected.
    jira_mock.get(ISSUE_PATH).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert TEST_JIRA_BASE_URL not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Hostile and broken payloads
# ---------------------------------------------------------------------------


async def test_non_json_with_a_200_becomes_an_upstream_error(jira_mock: respx.MockRouter):
    # Almost always an HTML login page served with a 200.
    jira_mock.get(ISSUE_PATH).respond(200, text="<html>Log in</html>")
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert "could not be parsed" in str(excinfo.value)


# `None` is absent on purpose: an empty body fails to parse as JSON and so
# takes the branch above, not this one. Both produce an UpstreamError.
@pytest.mark.parametrize("payload", [[], "a string", 42, True])
async def test_json_of_the_wrong_shape_becomes_an_upstream_error(
    jira_mock: respx.MockRouter, payload: object
):
    jira_mock.get(ISSUE_PATH).respond(200, json=payload)
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert "shape" in str(excinfo.value)


async def test_an_oversized_response_is_refused_before_parsing(jira_mock: respx.MockRouter):
    # Bounds what a hostile or broken upstream can make us buffer on the way
    # into a TEXT column.
    huge = '{"fields": {"summary": "' + "x" * (jira_client.MAX_RESPONSE_BYTES + 10) + '"}}'
    jira_mock.get(ISSUE_PATH).respond(200, text=huge, headers={"Content-Type": "application/json"})
    with pytest.raises(UpstreamError) as excinfo:
        await jira_client.fetch_issue("ABC-123")
    assert "too large" in str(excinfo.value)


async def test_no_log_line_contains_the_api_token(jira_mock: respx.MockRouter, log_buffer: Any):
    jira_mock.get(ISSUE_PATH).respond(401, json={})
    with pytest.raises(UpstreamError):
        await jira_client.fetch_issue("ABC-123")
    assert "jira-api-token-for-tests" not in log_buffer.getvalue()


async def test_closing_an_unbuilt_client_is_a_no_op():
    # Shutdown runs whether or not anything called Jira.
    jira_client.reset_http_client_cache()
    await jira_client.close_http_client()

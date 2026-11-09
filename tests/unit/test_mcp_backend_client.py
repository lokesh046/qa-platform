"""Tests for ``mcp_server.backend_client``.

This module's real job is translating a backend refusal into a sentence a
language model can act on, so most of these tests assert on message text. That
is usually a smell. Here it is the contract: the string goes to the model, the
model shows it to a person, and "502 Bad Gateway" helps nobody.

``_user_message`` gets a case per status because of how it failed in Step 7:
``httpx.codes.UNPROCESSABLE_CONTENT`` does not exist (httpx keeps
``UNPROCESSABLE_ENTITY`` while Starlette renamed it), the name is read inside
the function rather than at import, and the resulting ``AttributeError``
escaped as itself — so three of six refusal paths crashed *and* skipped their
audit write while the other three worked. A table of statuses is what catches
that class of bug.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from mcp_server.backend_client import BackendError, build_client, get_issue, post_audit
from tests.conftest import TEST_ACTOR, TEST_BACKEND_BASE_URL, TEST_TOKEN, make_mcp_settings

ISSUE_PATH = "/api/v1/issues/ABC-123"
AUDIT_PATH = "/api/v1/audit"
REQUEST_ID = "0123456789abcdef"


def issue_data(key: str = "ABC-123", *, from_cache: bool = False) -> dict[str, Any]:
    """The ``data`` member the backend returns for an issue."""
    return {
        "issue_key": key,
        "project_key": key.split("-", 1)[0],
        "summary": "Login rejects a valid password",
        "description": "A user cannot log in.",
        "acceptance_criteria": "- Valid password grants access",
        "issue_type": "Bug",
        "status": "To Do",
        "jira_updated_at": "2026-11-01T04:00:00Z",
        "fetched_at": "2026-11-08T12:00:00Z",
        "from_cache": from_cache,
    }


def envelope(
    data: dict[str, Any] | None = None, code: str | None = None, message: str | None = None
) -> dict[str, Any]:
    """The backend's response envelope, success or failure."""
    return {
        "data": data,
        "error": {"code": code, "message": message, "details": None} if code else None,
        "request_id": REQUEST_ID,
    }


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The pooled backend client, built from test settings."""
    http = build_client(make_mcp_settings())
    try:
        yield http
    finally:
        await http.aclose()


@pytest.fixture
def backend() -> Iterator[respx.MockRouter]:
    """Intercept every request to the backend."""
    with respx.mock(base_url=TEST_BACKEND_BASE_URL, assert_all_called=False) as mock:
        yield mock


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


async def test_the_service_token_is_a_default_header(client: httpx.AsyncClient):
    assert client.headers["authorization"] == f"Bearer {TEST_TOKEN}"


async def test_the_actor_is_not_a_default_header(client: httpx.AsyncClient):
    # A default actor header is a bug waiting to attribute one user's request
    # to another, because it changes per call and a default would persist.
    assert "x-actor-email" not in client.headers


async def test_the_client_does_not_follow_redirects(client: httpx.AsyncClient):
    assert client.follow_redirects is False


# ---------------------------------------------------------------------------
# get_issue: the happy path
# ---------------------------------------------------------------------------


async def test_a_successful_read_returns_the_envelope_data(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    data = await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)
    assert data["summary"] == "Login rejects a valid password"


async def test_the_call_carries_the_actor_and_the_request_id(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    # X-Request-ID is what correlates this call, the backend's log lines and
    # both audit rows.
    route = backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)
    request = route.calls[0].request
    assert request.headers["x-actor-email"] == TEST_ACTOR
    assert request.headers["x-request-id"] == REQUEST_ID
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


@pytest.mark.parametrize("data", [None, [], "a string", 42])
async def test_a_200_with_an_unexpected_shape_is_an_error(
    client: httpx.AsyncClient, backend: respx.MockRouter, data: object
):
    backend.get(ISSUE_PATH).respond(json={"data": data, "error": None, "request_id": REQUEST_ID})
    with pytest.raises(BackendError, match="unexpected response shape"):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


# ---------------------------------------------------------------------------
# get_issue: every refusal the backend can produce
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "backend_message", "expected"),
    [
        (404, "not_found", "Issue ABC-123 was not found.", "do not have access"),
        (403, "forbidden", "This account is deactivated.", "deactivated"),
        (401, "unauthorized", "Invalid service token.", "not authorised"),
        (422, "validation_error", "String should match pattern", "not a valid jira issue key"),
        (429, "rate_limited", "Jira is rate limiting this service.", "rate limiting"),
        (502, "upstream_error", "Could not reach Jira.", "could not reach jira"),
        (504, "upstream_timeout", "Jira did not respond in time.", "did not respond in time"),
        (500, "internal_error", "An internal error occurred.", "could not serve this request"),
        (409, "conflict", "Clashes with current state.", "could not serve this request"),
    ],
    ids=["404", "403", "401", "422", "429", "502", "504", "500", "409"],
)
async def test_each_status_becomes_a_usable_message(
    client: httpx.AsyncClient,
    backend: respx.MockRouter,
    status: int,
    code: str,
    backend_message: str,
    expected: str,
):
    # The 422, 429 and 504 rows are the regression test: those three were the
    # paths that reached the missing httpx constant.
    backend.get(ISSUE_PATH).respond(status, json=envelope(code=code, message=backend_message))
    with pytest.raises(BackendError) as excinfo:
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)
    assert expected in str(excinfo.value).lower()


async def test_no_status_raises_anything_other_than_backend_error(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    # The generalised form of the Step 7 bug: any status at all must produce a
    # BackendError, because the caller's audit branch catches only that.
    for status in [400, 402, 405, 410, 418, 423, 451, 500, 501, 503, 505, 507]:
        backend.get(ISSUE_PATH).respond(status, json=envelope(code="x", message="y"))
        with pytest.raises(BackendError):
            await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_the_404_message_covers_both_meanings(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    # The backend does not distinguish "no such issue" from "no access to that
    # project" (ADR-010), so this message must not appear to either.
    backend.get(ISSUE_PATH).respond(404, json=envelope(code="not_found", message="not found"))
    with pytest.raises(BackendError) as excinfo:
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)
    message = str(excinfo.value)
    assert "ABC-123 was not found, or you do not have access to its project." in message
    assert "grant you access" in message


async def test_a_401_does_not_blame_the_user(client: httpx.AsyncClient, backend: respx.MockRouter):
    # The end user cannot fix this; it is this server's configuration.
    backend.get(ISSUE_PATH).respond(401, json=envelope(code="unauthorized", message="nope"))
    with pytest.raises(BackendError) as excinfo:
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)
    assert "This MCP server is not authorised" in str(excinfo.value)


async def test_a_403_passes_the_backends_own_message_through(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    # 403 and 404 messages are already written for an end user.
    backend.get(ISSUE_PATH).respond(
        403, json=envelope(code="forbidden", message="This account is not registered.")
    )
    with pytest.raises(BackendError, match=r"This account is not registered\."):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_a_403_with_no_message_still_says_something(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    backend.get(ISSUE_PATH).respond(403, json={"data": None, "error": None, "request_id": "x"})
    with pytest.raises(BackendError, match="cannot use the QA platform"):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_an_error_body_of_the_wrong_shape_is_tolerated(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    for body in [{"error": "a string"}, {"error": []}, {}, [1, 2, 3]]:
        backend.get(ISSUE_PATH).respond(500, json=body)
        with pytest.raises(BackendError):
            await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


# ---------------------------------------------------------------------------
# get_issue: transport failures
# ---------------------------------------------------------------------------


async def test_a_timeout_says_to_try_again(client: httpx.AsyncClient, backend: respx.MockRouter):
    backend.get(ISSUE_PATH).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(BackendError, match="did not respond in time"):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_an_unreachable_backend_says_what_to_check(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    backend.get(ISSUE_PATH).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BackendError, match="unreachable"):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_non_json_is_reported_as_unreadable(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    backend.get(ISSUE_PATH).respond(200, text="<html>nginx</html>")
    with pytest.raises(BackendError, match="unreadable"):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_an_oversized_response_is_refused(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    from mcp_server.backend_client import MAX_RESPONSE_BYTES

    backend.get(ISSUE_PATH).respond(200, text="x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(BackendError, match="too large"):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)


async def test_no_log_line_contains_the_service_token(
    client: httpx.AsyncClient, backend: respx.MockRouter, log_buffer: io.StringIO
):
    backend.get(ISSUE_PATH).respond(401, json=envelope(code="unauthorized", message="nope"))
    with pytest.raises(BackendError):
        await get_issue(client, "ABC-123", TEST_ACTOR, REQUEST_ID)
    logged = log_buffer.getvalue()
    assert TEST_TOKEN not in logged
    assert "Bearer" not in logged


# ---------------------------------------------------------------------------
# post_audit
# ---------------------------------------------------------------------------


async def test_an_audit_write_sends_only_what_happened(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    # The actor, the source, the timestamp and the request id are all set by
    # the server, because all four are things a caller could otherwise forge.
    route = backend.post(AUDIT_PATH).respond(201, json=envelope({"recorded": True}))
    await post_audit(
        client,
        actor_email=TEST_ACTOR,
        request_id=REQUEST_ID,
        action="mcp.get_jira_issue",
        outcome="allowed",
        resource_type="jira_issue",
        resource_id="ABC-123",
        detail={"project_key": "ABC"},
    )
    import json

    body = json.loads(route.calls[0].request.content)
    assert set(body) == {"action", "outcome", "resource_type", "resource_id", "detail"}
    assert route.calls[0].request.headers["x-actor-email"] == TEST_ACTOR
    assert route.calls[0].request.headers["x-request-id"] == REQUEST_ID


@pytest.mark.parametrize("status", [200, 202, 400, 401, 422, 500])
async def test_anything_but_a_201_is_a_failed_audit_write(
    client: httpx.AsyncClient, backend: respx.MockRouter, status: int
):
    # A 200 is not success here: the endpoint returns 201, and accepting
    # anything 2xx would hide a route that silently stopped writing rows.
    backend.post(AUDIT_PATH).respond(status, json=envelope(code="internal_error", message="no"))
    with pytest.raises(BackendError, match="audit write refused"):
        await post_audit(
            client,
            actor_email=TEST_ACTOR,
            request_id=REQUEST_ID,
            action="mcp.get_jira_issue",
            outcome="allowed",
            resource_type="jira_issue",
            resource_id="ABC-123",
            detail={},
        )


async def test_a_failed_audit_write_names_the_status(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    backend.post(AUDIT_PATH).respond(503, json=envelope(code="service_unavailable", message="no"))
    with pytest.raises(BackendError) as excinfo:
        await post_audit(
            client,
            actor_email=TEST_ACTOR,
            request_id=REQUEST_ID,
            action="mcp.get_jira_issue",
            outcome="denied",
            resource_type="jira_issue",
            resource_id=None,
            detail={},
        )
    assert "503" in str(excinfo.value)
    assert "service_unavailable" in str(excinfo.value)


async def test_a_failed_audit_write_with_an_empty_body_does_not_crash(
    client: httpx.AsyncClient, backend: respx.MockRouter
):
    # An empty body is what a proxy returns on a 502. Parsing it as JSON would
    # raise inside the error path and replace a clear failure with a confusing
    # one.
    backend.post(AUDIT_PATH).respond(502)
    with pytest.raises(BackendError, match="audit write refused"):
        await post_audit(
            client,
            actor_email=TEST_ACTOR,
            request_id=REQUEST_ID,
            action="mcp.get_jira_issue",
            outcome="error",
            resource_type="jira_issue",
            resource_id="ABC-123",
            detail={},
        )

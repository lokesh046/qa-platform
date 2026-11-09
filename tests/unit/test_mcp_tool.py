"""Tests for the ``get_jira_issue`` tool, driven through the MCP SDK.

These go through the SDK's in-process ``Client`` rather than calling the
handler function directly, and that is the whole point of the file. Two
properties only exist on the far side of the SDK:

- **A message reaches the model only if it was raised as ``ToolError``.** Any
  other exception becomes a bare "Error executing tool get_jira_issue" logged
  as a crash, discarding whatever the handler tried to say. Calling the
  handler directly would assert on the message the code *meant* to send, which
  is exactly the assertion that passed while the bug was live.
- **What the model is told about the tool** — its name, description and input
  schema — which is what decides whether it gets called correctly at all.

The backend is mocked with ``respx``, so nothing here needs the API running.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from mcp import Client

from mcp_server.config import McpSettings
from mcp_server.server import build_server
from tests.conftest import TEST_ACTOR, TEST_BACKEND_BASE_URL, TEST_TOKEN, make_mcp_settings

ISSUE_PATH = "/api/v1/issues/ABC-123"
AUDIT_PATH = "/api/v1/audit"


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
    """The backend's response envelope."""
    return {
        "data": data,
        "error": {"code": code, "message": message, "details": None} if code else None,
        "request_id": "fixed",
    }


def text_of(result: Any) -> str:
    """Join the text blocks of a ``CallToolResult``."""
    return "\n".join(getattr(block, "text", "") or "" for block in result.content)


def audit_bodies(route: respx.Route) -> list[dict[str, Any]]:
    """Parse every audit record posted through a route."""
    return [json.loads(call.request.content) for call in route.calls]


@pytest.fixture
def backend() -> Iterator[respx.MockRouter]:
    """Intercept every request to the backend."""
    with respx.mock(base_url=TEST_BACKEND_BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture(autouse=True)
def audit(backend: respx.MockRouter) -> respx.Route:
    """The audit endpoint, succeeding by default.

    Autouse, because every tool call writes a record and a test that left this
    route unregistered would exercise the fail-closed path by accident. The
    first draft of this file did exactly that: ``respx`` answered the
    unregistered POST with a 200, ``post_audit`` requires a 201, and the
    resulting failures looked like bugs in the tool.

    A test asks for it by name to assert on what was recorded, or to make the
    write fail with ``audit.respond(...)``.
    """
    return backend.post(AUDIT_PATH).respond(201, json=envelope({"recorded": True}))


@asynccontextmanager
async def tool_client(settings: McpSettings | None = None) -> AsyncIterator[Client]:
    """Connect an MCP client to the server over the SDK's in-process transport.

    A helper rather than a fixture, and not by preference. pytest-asyncio runs
    an async fixture's setup and teardown in two different tasks, while the
    SDK's client holds an anyio cancel scope that must be exited in the task
    that entered it — so a fixture version raises "Attempted to exit cancel
    scope in a different task" as every test using it tears down. Entering it
    inside the test body keeps both halves in one task.
    """
    http = httpx.AsyncClient(
        base_url=TEST_BACKEND_BASE_URL, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )
    try:
        async with Client(build_server(settings or make_mcp_settings(), http)) as client:
            yield client
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------


async def test_exactly_one_tool_is_advertised():
    async with tool_client() as tool:
        listed = await tool.list_tools()
    assert [t.name for t in listed.tools] == ["get_jira_issue"]


async def test_the_description_states_the_key_format():
    # A model that guesses an argument format produces a confident error
    # message instead of an answer.
    async with tool_client() as tool:
        description = (await tool.list_tools()).tools[0].description or ""
    assert "ABC-123" in description
    assert "uppercase" in description


async def test_the_description_says_not_to_retry_a_not_found():
    async with tool_client() as tool:
        description = (await tool.list_tools()).tools[0].description or ""
    assert "not distinguished" in description
    assert "Do not retry" in description


async def test_the_description_says_what_is_not_returned():
    # Otherwise the model asks for comments and attachments, and invents them
    # when they do not arrive.
    async with tool_client() as tool:
        description = (await tool.list_tools()).tools[0].description or ""
    assert "does not return comments" in description


async def test_issue_key_is_the_only_argument():
    async with tool_client() as tool:
        schema = (await tool.list_tools()).tools[0].input_schema
    assert list(schema["properties"]) == ["issue_key"]
    assert schema["required"] == ["issue_key"]


async def test_the_context_parameter_is_not_exposed_as_an_argument():
    async with tool_client() as tool:
        schema = (await tool.list_tools()).tools[0].input_schema
    assert "ctx" not in json.dumps(schema)


async def test_the_schema_does_not_carry_the_pattern():
    # Deliberate. With the regex on the annotation the SDK would reject a bad
    # key before the handler runs, so no audit record would be written — and a
    # run of malformed calls from one actor is exactly what the trail is for.
    # The description still tells the model the format.
    async with tool_client() as tool:
        schema = (await tool.list_tools()).tools[0].input_schema
    assert "pattern" not in json.dumps(schema)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_successful_call_returns_the_summary_and_criteria(backend: respx.MockRouter):
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert not result.is_error
    payload = json.loads(text_of(result))
    assert payload["summary"] == "Login rejects a valid password"
    assert payload["acceptance_criteria"] == "- Valid password grants access"
    assert payload["project_key"] == "ABC"


@pytest.mark.parametrize("field", ["description", "fetched_at"])
async def test_the_response_is_narrowed_further_than_the_backends(
    backend: respx.MockRouter, field: str
):
    # The backend already returns a small projection (ADR-009); the tool
    # narrows it to what the brief asks for plus the context a reader needs.
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert field not in json.loads(text_of(result))


async def test_the_backend_is_called_with_the_resolved_actor(backend: respx.MockRouter):
    route = backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    async with tool_client() as tool:
        await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert route.calls[0].request.headers["x-actor-email"] == TEST_ACTOR
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


async def test_the_issue_call_and_its_audit_row_share_a_request_id(
    backend: respx.MockRouter, audit: respx.Route
):
    # One id per tool call, so an administrator can line up the two rows and
    # both processes' log lines.
    issue = backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    async with tool_client() as tool:
        await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert (
        audit.calls[0].request.headers["x-request-id"]
        == issue.calls[0].request.headers["x-request-id"]
    )


async def test_a_successful_call_is_audited_as_allowed(
    backend: respx.MockRouter, audit: respx.Route
):
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data(from_cache=True)))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert not result.is_error
    body = audit_bodies(audit)[0]
    assert body["action"] == "mcp.get_jira_issue"
    assert body["outcome"] == "allowed"
    assert body["resource_id"] == "ABC-123"
    assert body["detail"]["project_key"] == "ABC"
    assert body["detail"]["from_cache"] is True


async def test_the_audit_body_omits_every_server_set_field(
    backend: respx.MockRouter, audit: respx.Route
):
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    async with tool_client() as tool:
        await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    body = audit_bodies(audit)[0]
    for forged in ("actor_email", "source", "occurred_at", "request_id"):
        assert forged not in body


# ---------------------------------------------------------------------------
# Argument validation, and the fact that it is audited
# ---------------------------------------------------------------------------

MALFORMED_KEYS = [
    "abc-123",
    "ABC123",
    "ABC-",
    "A-1",
    "ABC-1x",
    "ABC-1;DROP TABLE users",
    "https://acme-qa.atlassian.net/browse/ABC-1",
    "",
]


@pytest.mark.parametrize("key", MALFORMED_KEYS)
async def test_a_malformed_key_is_an_error_with_the_expected_format(key: str):
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": key})
    assert result.is_error
    # The message must survive the SDK boundary, which is only true for
    # ToolError. This assertion is the regression test for that bug: raised as
    # a ValueError, the model receives "Error executing tool get_jira_issue".
    assert "ABC-123" in text_of(result)


async def test_a_malformed_key_costs_no_backend_request(backend: respx.MockRouter):
    issues = backend.get(url__startswith=f"{TEST_BACKEND_BASE_URL}/api/v1/issues/")
    async with tool_client() as tool:
        for key in MALFORMED_KEYS:
            await tool.call_tool("get_jira_issue", {"issue_key": key})
    assert issues.call_count == 0


async def test_every_rejected_argument_is_audited_as_denied(audit: respx.Route):
    # A run of malformed calls from one actor is exactly the kind of probing
    # the gatekeeper exists to show.
    async with tool_client() as tool:
        for key in MALFORMED_KEYS:
            await tool.call_tool("get_jira_issue", {"issue_key": key})
    bodies = audit_bodies(audit)
    assert len(bodies) == len(MALFORMED_KEYS)
    assert {body["outcome"] for body in bodies} == {"denied"}


async def test_a_rejection_records_the_rule_and_not_the_value(audit: respx.Route):
    # The failing value is model-supplied free text; ADR-009 forbids storing
    # it verbatim.
    async with tool_client() as tool:
        await tool.call_tool("get_jira_issue", {"issue_key": "ABC-1;DROP TABLE users"})
    body = audit_bodies(audit)[0]
    assert body["detail"]["reason"] == "issue_key:string_pattern_mismatch"
    assert body["resource_id"] is None
    assert "DROP TABLE" not in json.dumps(body)


async def test_an_unknown_argument_is_rejected():
    # A silently ignored argument becomes a confidently wrong answer.
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-1", "fields": "all"})
    assert result.is_error


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


async def test_a_call_with_no_identity_is_refused_before_the_arguments(audit: respx.Route):
    # Refused before the arguments are even looked at, and not audited: there
    # is no actor to attribute a record to, and the backend would reject a row
    # without one.
    settings = make_mcp_settings(mcp_transport="http", qa_actor_email=None)
    async with tool_client(settings) as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error
    assert "Authorization" in text_of(result)
    assert audit.call_count == 0


async def test_an_http_server_refuses_a_malformed_key_on_identity_first(audit: respx.Route):
    # Over the in-process transport no headers are delivered, so an http
    # server refuses on identity before it ever looks at the key. Checked so
    # that neither transport can start answering calls it should not.
    settings = make_mcp_settings(mcp_transport="http", qa_actor_email=None)
    async with tool_client(settings) as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "nonsense"})
    assert result.is_error
    assert audit.call_count == 0


# ---------------------------------------------------------------------------
# Backend refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (404, "not_found", "do not have access"),
        (403, "forbidden", "deactivated"),
        (401, "unauthorized", "not authorised"),
        (422, "validation_error", "not a valid jira issue key"),
        (429, "rate_limited", "rate limiting"),
        (502, "upstream_error", "could not reach jira"),
        (504, "upstream_timeout", "did not respond in time"),
    ],
    ids=["404", "403", "401", "422", "429", "502", "504"],
)
async def test_a_backend_refusal_reaches_the_model_as_a_message(
    backend: respx.MockRouter, status: int, code: str, expected: str
):
    backend.get(ISSUE_PATH).respond(
        status, json=envelope(code=code, message="This account is deactivated.")
    )
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error
    assert expected in text_of(result).lower()


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 502, 504, 500])
async def test_every_backend_refusal_is_audited(
    backend: respx.MockRouter, audit: respx.Route, status: int
):
    # The generalised form of the Step 7 bug: three of six refusals wrote no
    # audit row at all, because an AttributeError escaped the branch that was
    # supposed to record them.
    backend.get(ISSUE_PATH).respond(status, json=envelope(code="x", message="y"))
    async with tool_client() as tool:
        await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    bodies = audit_bodies(audit)
    assert len(bodies) == 1
    assert bodies[0]["outcome"] == "error"
    assert bodies[0]["detail"]["reason"] == "backend_refused"
    assert bodies[0]["detail"]["project_key"] == "ABC"


async def test_an_unreachable_backend_is_reported_and_audited(
    backend: respx.MockRouter, audit: respx.Route
):
    backend.get(ISSUE_PATH).mock(side_effect=httpx.ConnectError("refused"))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error
    assert "unreachable" in text_of(result).lower()
    assert audit.call_count == 1


async def test_an_unexpected_internal_failure_still_writes_a_record(
    audit: respx.Route, monkeypatch: pytest.MonkeyPatch
):
    # The branch that exists because of the Step 7 bug. An AttributeError
    # inside the backend client's message builder escaped as itself, the
    # BackendError branch never ran, and the call produced no audit row. The
    # trail must not depend on the rest of that function being correct.
    async def exploding_get_issue(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AttributeError("module 'httpx.codes' has no attribute 'IMAGINARY'")

    monkeypatch.setattr("mcp_server.server.get_issue", exploding_get_issue)
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error
    bodies = audit_bodies(audit)
    assert len(bodies) == 1
    assert bodies[0]["outcome"] == "error"
    assert bodies[0]["detail"]["reason"] == "internal_error"
    assert bodies[0]["detail"]["error_type"] == "AttributeError"


async def test_an_internal_failure_does_not_leak_its_message_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
):
    # Re-raised unchanged, so the SDK treats it as a crash and the model gets a
    # generic message. An internal fault is not something it can act on, and
    # exception text routinely contains paths, SQL fragments and occasionally
    # connection strings.
    async def exploding_get_issue(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("connection string postgresql://qa:hunter2@db/qa failed")

    monkeypatch.setattr("mcp_server.server.get_issue", exploding_get_issue)
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error
    assert "hunter2" not in text_of(result)


# ---------------------------------------------------------------------------
# Fail-closed auditing
# ---------------------------------------------------------------------------


async def test_a_call_fails_when_its_audit_record_cannot_be_written(
    backend: respx.MockRouter, audit: respx.Route
):
    # ADR-013. A gatekeeper that cannot record what it did should not quietly
    # carry on, and this matches the backend's own behaviour so the two layers
    # fail the same way.
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    audit.respond(500, json=envelope(code="internal_error", message="no"))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error


async def test_a_denial_that_cannot_be_audited_also_fails(audit: respx.Route):
    audit.respond(500, json=envelope(code="internal_error", message="no"))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "not-a-key"})
    assert result.is_error


async def test_a_successful_read_is_withheld_if_it_cannot_be_recorded(
    backend: respx.MockRouter, audit: respx.Route
):
    # The strongest form of the rule: the data was fetched successfully and the
    # call still fails, because the alternative is serving a read the trail
    # does not know about.
    backend.get(ISSUE_PATH).respond(json=envelope(issue_data()))
    audit.respond(503, json=envelope(code="service_unavailable", message="no"))
    async with tool_client() as tool:
        result = await tool.call_tool("get_jira_issue", {"issue_key": "ABC-123"})
    assert result.is_error
    assert "Login rejects a valid password" not in text_of(result)

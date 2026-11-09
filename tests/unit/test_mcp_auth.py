"""Tests for ``mcp_server.auth``: two transports, two trust models.

The asymmetry under test: on stdio the process boundary *is* the identity, so
the actor comes from the environment; on http a header is client-supplied
input, so the shared service token must be presented first and
``QA_ACTOR_EMAIL`` is ignored entirely.

Each refusal is checked for *which* message it gives, because the message is
what a Copilot Chat user sees and "Token rejected" versus "requires an
X-Actor-Email header" is the difference between a fixable problem and a
mystery.
"""

from __future__ import annotations

import io

import pytest

from mcp_server.auth import CallerRefusedError, check_startup_identity, resolve_actor
from mcp_server.config import McpSettings
from tests.conftest import TEST_ACTOR, TEST_TOKEN, make_mcp_settings

HTTP_HEADERS = {"authorization": f"Bearer {TEST_TOKEN}", "x-actor-email": TEST_ACTOR}


@pytest.fixture
def http_settings() -> McpSettings:
    """An http-transport server with no process-wide actor."""
    return make_mcp_settings(mcp_transport="http", qa_actor_email=None)


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------


def test_stdio_takes_the_actor_from_the_environment(mcp_settings: McpSettings):
    assert resolve_actor(None, mcp_settings) == TEST_ACTOR


def test_stdio_canonicalises_the_actor():
    # Must agree with the backend, which lowercases the actor header. See
    # ADR-017.
    settings = make_mcp_settings(qa_actor_email="QA.One@Acme-QA.co")
    assert resolve_actor(None, settings) == TEST_ACTOR


def test_stdio_without_an_actor_is_refused_with_what_to_set():
    settings = make_mcp_settings(qa_actor_email=None)
    with pytest.raises(CallerRefusedError) as excinfo:
        resolve_actor(None, settings)
    assert "QA_ACTOR_EMAIL" in str(excinfo.value)


def test_stdio_refuses_a_call_that_arrives_with_headers(mcp_settings: McpSettings):
    # Belt and braces. If a future transport delivers headers while the
    # process believes it is on stdio, the environment identity would be
    # applied to a request from somewhere else.
    with pytest.raises(CallerRefusedError) as excinfo:
        resolve_actor({"x-actor-email": "someone.else@acme-qa.co"}, mcp_settings)
    assert "transport" in str(excinfo.value)


def test_stdio_refuses_even_an_empty_header_mapping(mcp_settings: McpSettings):
    # An empty dict is still "headers were delivered", which is the signal.
    with pytest.raises(CallerRefusedError):
        resolve_actor({}, mcp_settings)


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------


def test_http_reads_the_actor_from_the_header(http_settings: McpSettings):
    assert resolve_actor(HTTP_HEADERS, http_settings) == TEST_ACTOR


def test_http_header_lookup_is_case_insensitive(http_settings: McpSettings):
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-Email": TEST_ACTOR}
    assert resolve_actor(headers, http_settings) == TEST_ACTOR


def test_http_accepts_a_lowercase_bearer_scheme(http_settings: McpSettings):
    headers = {"authorization": f"bearer {TEST_TOKEN}", "x-actor-email": TEST_ACTOR}
    assert resolve_actor(headers, http_settings) == TEST_ACTOR


@pytest.mark.parametrize(
    ("headers", "expected_fragment"),
    [
        (None, "Authorization"),
        ({}, "Authorization"),
        ({"x-actor-email": TEST_ACTOR}, "Authorization"),
        ({"authorization": f"Basic {TEST_TOKEN}", "x-actor-email": TEST_ACTOR}, "Bearer"),
        ({"authorization": "Bearer", "x-actor-email": TEST_ACTOR}, "Bearer"),
        ({"authorization": "Bearer wrong-token", "x-actor-email": TEST_ACTOR}, "rejected"),
        ({"authorization": f"Bearer {TEST_TOKEN[:20]}", "x-actor-email": TEST_ACTOR}, "rejected"),
        ({"authorization": f"Bearer {TEST_TOKEN}x", "x-actor-email": TEST_ACTOR}, "rejected"),
        ({"authorization": f"Bearer {TEST_TOKEN}"}, "X-Actor-Email"),
        ({"authorization": f"Bearer {TEST_TOKEN}", "x-actor-email": ""}, "X-Actor-Email"),
        ({"authorization": f"Bearer {TEST_TOKEN}", "x-actor-email": "nope"}, "valid address"),
    ],
    ids=[
        "no headers",
        "empty headers",
        "no token",
        "non-bearer scheme",
        "bearer with no token",
        "wrong token",
        "a correct prefix only",
        "the token plus a character",
        "token but no actor",
        "token but an empty actor",
        "token but a malformed actor",
    ],
)
def test_http_refusals(http_settings: McpSettings, headers: object, expected_fragment: str):
    with pytest.raises(CallerRefusedError) as excinfo:
        resolve_actor(headers, http_settings)
    assert expected_fragment.lower() in str(excinfo.value).lower()


def test_the_token_is_checked_before_the_actor_header_is_read(http_settings: McpSettings):
    # The order is the security property: reading the actor first would let an
    # unauthenticated caller choose whose name ends up in the audit log.
    with pytest.raises(CallerRefusedError) as excinfo:
        resolve_actor(
            {"authorization": "Bearer wrong", "x-actor-email": "victim@acme-qa.co"},
            http_settings,
        )
    assert "rejected" in str(excinfo.value)


def test_http_ignores_the_process_wide_actor():
    # A container that starts cleanly and attributes every call to one person
    # is the worst available outcome, because it looks like it is working.
    settings = make_mcp_settings(mcp_transport="http", qa_actor_email="someone.else@acme-qa.co")
    assert resolve_actor(HTTP_HEADERS, settings) == TEST_ACTOR


def test_no_refusal_message_quotes_the_expected_token(http_settings: McpSettings):
    # "token rejected", never "expected a token starting with".
    for headers in ({"authorization": "Bearer wrong", "x-actor-email": TEST_ACTOR}, None, {}):
        with pytest.raises(CallerRefusedError) as excinfo:
            resolve_actor(headers, http_settings)
        assert TEST_TOKEN not in str(excinfo.value)
        assert TEST_TOKEN[:8] not in str(excinfo.value)


def test_a_rejected_token_is_logged_without_any_of_it(
    http_settings: McpSettings, log_buffer: io.StringIO
):
    # The length is not logged either: it narrows a guess.
    with pytest.raises(CallerRefusedError):
        resolve_actor(
            {"authorization": "Bearer wrong-token", "x-actor-email": TEST_ACTOR}, http_settings
        )
    logged = log_buffer.getvalue()
    assert "token rejected" in logged
    assert TEST_TOKEN not in logged
    assert "wrong-token" not in logged


def test_a_non_mapping_headers_object_is_refused_not_crashed(http_settings: McpSettings):
    # ctx.headers is whatever the SDK hands over. If it is ever not a mapping,
    # this must refuse rather than raise AttributeError inside the handler.
    with pytest.raises(CallerRefusedError):
        resolve_actor("not a mapping", http_settings)


def test_a_non_string_header_value_is_ignored(http_settings: McpSettings):
    with pytest.raises(CallerRefusedError):
        resolve_actor({"authorization": 1234, "x-actor-email": TEST_ACTOR}, http_settings)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_stdio_refuses_to_start_without_an_identity():
    # The alternative is a process that accepts connections and then refuses
    # every call.
    with pytest.raises(CallerRefusedError) as excinfo:
        check_startup_identity(make_mcp_settings(qa_actor_email=None))
    assert "MCP_TRANSPORT=stdio" in str(excinfo.value)


def test_stdio_starts_with_an_identity(mcp_settings: McpSettings):
    check_startup_identity(mcp_settings)


def test_http_starts_without_a_process_wide_identity(http_settings: McpSettings):
    check_startup_identity(http_settings)


def test_http_warns_when_the_process_wide_actor_is_set(log_buffer: io.StringIO):
    # Not fatal, but somebody expects it to be used, and saying so is cheaper
    # than the support conversation about why every audit row names one person.
    check_startup_identity(
        make_mcp_settings(mcp_transport="http", qa_actor_email="someone@acme-qa.co")
    )
    assert "ignored on the http transport" in log_buffer.getvalue()

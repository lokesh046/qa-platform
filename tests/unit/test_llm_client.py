"""Tests for ``app.llm.client``: the gateway to an OpenAI-compatible provider.

The provider is mocked with ``respx``, so nothing here resolves a hostname,
opens a socket, or costs money. A test suite that charges per run is a test
suite people stop running (ADR-018, ADR-023).

Every status gets a case. The reason is the same one that caught the Step 7
bug: a branch reached only by some statuses can be wrong for months, and the
day a provider first returns a 429 is not the day to find out it was mapped to
"upstream error" and read like a bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import (
    RateLimitedError,
    ServiceUnavailableError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.llm import client as llm_client
from tests.conftest import (
    TEST_LLM_API_KEY,
    TEST_LLM_BASE_URL,
    TEST_LLM_MODEL,
    make_llm_settings,
    make_settings,
)

COMPLETIONS_PATH = "/chat/completions"


def completion_payload(
    text: str = "## ADDED Requirements\n",
    *,
    finish_reason: str = "stop",
    model: str | None = TEST_LLM_MODEL,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A reply in the shape an OpenAI-compatible provider returns."""
    body: dict[str, Any] = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage if usage is not None else {"prompt_tokens": 120, "completion_tokens": 340},
    }
    if model is not None:
        body["model"] = model
    return body


@pytest.fixture
def llm_settings() -> Settings:
    """Settings with generation configured."""
    return make_llm_settings()


@pytest.fixture
def provider() -> Iterator[respx.MockRouter]:
    """Intercept every request to the provider host."""
    with respx.mock(base_url=TEST_LLM_BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def _pooled_client(
    llm_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Point the module's cached client at the test settings, then clean up.

    ``get_http_client`` is an ``lru_cache``, so without this a test would reuse
    another test's client or build one from the developer's ``.env``.
    """
    monkeypatch.setattr("app.llm.client.get_settings", lambda: llm_settings)
    llm_client.reset_http_client_cache()
    try:
        yield
    finally:
        await llm_client.close_http_client()
        llm_client.reset_http_client_cache()


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of waiting them out.

    Returned so a test can assert the backoff *schedule*, which is the part
    worth pinning: retries that all fire immediately are not retries.
    """
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("app.llm.client.asyncio.sleep", fake_sleep)
    return recorded


pytestmark = pytest.mark.usefixtures("_pooled_client")


async def ask(settings: Settings | None = None) -> llm_client.Completion:
    """Call ``generate`` with a fixed prompt."""
    return await llm_client.generate(
        system="You write requirements.", user="Issue SCRUM-1", settings=settings
    )


# ---------------------------------------------------------------------------
# The happy path, and what it reports
# ---------------------------------------------------------------------------


async def test_a_completion_is_returned(provider: respx.MockRouter, llm_settings: Settings):
    provider.post(COMPLETIONS_PATH).respond(json=completion_payload("## ADDED Requirements\n"))
    result = await ask(llm_settings)
    assert result.text == "## ADDED Requirements\n"
    assert result.finish_reason == "stop"
    assert result.was_truncated is False


async def test_token_usage_is_reported(provider: respx.MockRouter, llm_settings: Settings):
    # Recorded on the document so spend is attributable per issue, not just as
    # one monthly figure (ADR-023).
    provider.post(COMPLETIONS_PATH).respond(json=completion_payload())
    result = await ask(llm_settings)
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 340


async def test_the_model_that_answered_is_reported_not_assumed(
    provider: respx.MockRouter, llm_settings: Settings
):
    # A gateway may route elsewhere, and LLM_MODEL may name an alias.
    provider.post(COMPLETIONS_PATH).respond(json=completion_payload(model="other/model-v2"))
    assert (await ask(llm_settings)).model == "other/model-v2"


async def test_a_reply_without_a_model_falls_back_to_the_configured_one(
    provider: respx.MockRouter, llm_settings: Settings
):
    provider.post(COMPLETIONS_PATH).respond(json=completion_payload(model=None))
    assert (await ask(llm_settings)).model == TEST_LLM_MODEL


async def test_truncation_is_visible_to_the_caller(
    provider: respx.MockRouter, llm_settings: Settings
):
    # The load-bearing one. A cut-off requirements document is not a short one,
    # and nothing downstream may store it as though it were complete.
    provider.post(COMPLETIONS_PATH).respond(json=completion_payload(finish_reason="length"))
    result = await ask(llm_settings)
    assert result.was_truncated is True


async def test_the_request_carries_the_prompt_and_the_model(
    provider: respx.MockRouter, llm_settings: Settings
):
    route = provider.post(COMPLETIONS_PATH).respond(json=completion_payload())
    await ask(llm_settings)
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == TEST_LLM_MODEL
    assert body["max_tokens"] == llm_settings.llm_max_tokens
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][1]["content"] == "Issue SCRUM-1"


async def test_the_credential_is_sent_as_a_bearer_token(
    provider: respx.MockRouter, llm_settings: Settings
):
    route = provider.post(COMPLETIONS_PATH).respond(json=completion_payload())
    await ask(llm_settings)
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TEST_LLM_API_KEY}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", ""), (None, ""), (42, "")],
    ids=["empty-string", "null-content", "content-not-a-string"],
)
async def test_an_absent_reply_becomes_an_empty_string(
    provider: respx.MockRouter, llm_settings: Settings, text: object, expected: str
):
    # One empty case for the caller to handle, not three.
    payload = completion_payload()
    payload["choices"][0]["message"]["content"] = text
    provider.post(COMPLETIONS_PATH).respond(json=payload)
    assert (await ask(llm_settings)).text == expected


@pytest.mark.parametrize(
    "usage",
    [{}, {"prompt_tokens": "many"}, {"prompt_tokens": -1}, {"prompt_tokens": True}],
    ids=["absent", "not-an-int", "negative", "a-bool"],
)
async def test_an_odd_usage_block_does_not_fail_the_generation(
    provider: respx.MockRouter, llm_settings: Settings, usage: dict[str, Any]
):
    # Token counts are advisory and providers differ. A missing count must not
    # fail a generation that otherwise worked.
    provider.post(COMPLETIONS_PATH).respond(json=completion_payload(usage=usage))
    assert (await ask(llm_settings)).prompt_tokens is None


# ---------------------------------------------------------------------------
# Not configured: refused before any network call
# ---------------------------------------------------------------------------


async def test_generation_without_a_model_is_refused_before_calling_out(
    provider: respx.MockRouter,
):
    route = provider.post(COMPLETIONS_PATH).respond(json=completion_payload())
    with pytest.raises(ServiceUnavailableError, match="LLM_MODEL"):
        await ask(make_settings(llm_api_key=TEST_LLM_API_KEY))
    assert route.call_count == 0


async def test_generation_without_a_key_is_refused_before_calling_out(
    provider: respx.MockRouter,
):
    route = provider.post(COMPLETIONS_PATH).respond(json=completion_payload())
    with pytest.raises(ServiceUnavailableError, match="LLM_API_KEY"):
        await ask(make_settings(llm_model=TEST_LLM_MODEL))
    assert route.call_count == 0


async def test_the_unconfigured_message_names_both_missing_values():
    with pytest.raises(ServiceUnavailableError) as excinfo:
        await ask(make_settings())
    message = str(excinfo.value)
    assert "LLM_MODEL" in message
    assert "LLM_API_KEY" in message
    # And says where to find a model name, which is the next question.
    assert "catalog/models" in message


# ---------------------------------------------------------------------------
# One case per status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_credential_names_the_variable_to_fix(
    provider: respx.MockRouter, llm_settings: Settings, status: int
):
    provider.post(COMPLETIONS_PATH).respond(status)
    with pytest.raises(UpstreamError, match="LLM_API_KEY"):
        await ask(llm_settings)


async def test_a_404_blames_the_model_name(provider: respx.MockRouter, llm_settings: Settings):
    # On an OpenAI-compatible endpoint a 404 nearly always means the model name
    # is wrong. Saying "not found" would send someone to check the URL.
    provider.post(COMPLETIONS_PATH).respond(404)
    with pytest.raises(UpstreamError) as excinfo:
        await ask(llm_settings)
    assert TEST_LLM_MODEL in str(excinfo.value)
    assert "catalog/models" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 405, 409, 413, 422])
async def test_a_client_error_is_an_upstream_error_and_is_not_retried(
    provider: respx.MockRouter, llm_settings: Settings, status: int
):
    route = provider.post(COMPLETIONS_PATH).respond(status)
    with pytest.raises(UpstreamError):
        await ask(llm_settings)
    # A 4xx is a request this code built wrongly; it will build it wrongly again.
    assert route.call_count == 1


async def test_a_rate_limit_is_its_own_error(provider: respx.MockRouter, llm_settings: Settings):
    # Distinct from UpstreamError on purpose: the fix is a quota decision, not
    # a bug hunt, and GitHub Models' free tier will produce this (ADR-023).
    provider.post(COMPLETIONS_PATH).respond(429)
    with pytest.raises(RateLimitedError, match="rate limit"):
        await ask(llm_settings)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_server_error_is_retried_then_raised(
    provider: respx.MockRouter, llm_settings: Settings, status: int
):
    route = provider.post(COMPLETIONS_PATH).respond(status)
    with pytest.raises(UpstreamError):
        await ask(llm_settings)
    assert route.call_count == llm_settings.llm_max_retries + 1


async def test_a_retryable_status_that_then_succeeds_returns_the_completion(
    provider: respx.MockRouter, llm_settings: Settings
):
    route = provider.post(COMPLETIONS_PATH)
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=completion_payload("## ADDED Requirements\n")),
    ]
    result = await ask(llm_settings)
    assert result.text == "## ADDED Requirements\n"
    assert route.call_count == 2


async def test_the_backoff_grows(
    provider: respx.MockRouter, llm_settings: Settings, _no_real_sleeping: list[float]
):
    provider.post(COMPLETIONS_PATH).respond(503)
    with pytest.raises(UpstreamError):
        await ask(llm_settings)
    # Two retries by default, so two waits, and the second is longer.
    assert _no_real_sleeping == [0.5, 1.0]


async def test_retries_can_be_switched_off(provider: respx.MockRouter):
    route = provider.post(COMPLETIONS_PATH).respond(503)
    with pytest.raises(UpstreamError):
        await ask(make_llm_settings(llm_max_retries=0))
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


async def test_a_timeout_is_retried_then_reported_as_a_timeout(
    provider: respx.MockRouter, llm_settings: Settings
):
    route = provider.post(COMPLETIONS_PATH)
    route.side_effect = httpx.ReadTimeout("too slow")
    with pytest.raises(UpstreamTimeoutError, match="did not respond in time"):
        await ask(llm_settings)
    assert route.call_count == llm_settings.llm_max_retries + 1


async def test_an_unreachable_provider_is_reported_as_unreachable(
    provider: respx.MockRouter, llm_settings: Settings
):
    provider.post(COMPLETIONS_PATH).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(UpstreamError, match="Could not reach"):
        await ask(llm_settings)


# ---------------------------------------------------------------------------
# Malformed success responses
# ---------------------------------------------------------------------------


async def test_a_non_json_body_is_an_upstream_error(
    provider: respx.MockRouter, llm_settings: Settings
):
    provider.post(COMPLETIONS_PATH).respond(200, text="<html>gateway</html>")
    with pytest.raises(UpstreamError, match="could not be parsed"):
        await ask(llm_settings)


@pytest.mark.parametrize(
    "body",
    [[], "a string", 42, True],
    ids=["a-list", "a-string", "a-number", "a-bool"],
)
async def test_a_body_that_is_not_an_object_is_an_upstream_error(
    provider: respx.MockRouter, llm_settings: Settings, body: object
):
    provider.post(COMPLETIONS_PATH).respond(200, json=body)
    with pytest.raises(UpstreamError, match="unexpected response shape"):
        await ask(llm_settings)


@pytest.mark.parametrize(
    "choices",
    [None, [], "not a list", [None], ["not an object"]],
    ids=["absent", "empty", "not-a-list", "null-choice", "choice-not-an-object"],
)
async def test_a_reply_with_no_usable_choice_is_an_upstream_error(
    provider: respx.MockRouter, llm_settings: Settings, choices: object
):
    provider.post(COMPLETIONS_PATH).respond(200, json={"choices": choices})
    with pytest.raises(UpstreamError, match="no completion"):
        await ask(llm_settings)


async def test_an_oversized_reply_is_refused(provider: respx.MockRouter, llm_settings: Settings):
    # max_tokens bounds a well-behaved provider. This bounds the other kind.
    provider.post(COMPLETIONS_PATH).respond(
        200, json=completion_payload("x" * (llm_client.MAX_RESPONSE_BYTES + 100))
    )
    with pytest.raises(UpstreamError, match="too large"):
        await ask(llm_settings)


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


async def test_the_api_key_appears_in_no_log_line(
    provider: respx.MockRouter, llm_settings: Settings, log_buffer: Any
):
    provider.post(COMPLETIONS_PATH).respond(500)
    with pytest.raises(UpstreamError):
        await ask(llm_settings)
    assert TEST_LLM_API_KEY not in log_buffer.getvalue()


@pytest.mark.parametrize("status", [401, 403, 404, 400, 429, 500])
async def test_the_api_key_appears_in_no_error_message(
    provider: respx.MockRouter, llm_settings: Settings, status: int
):
    provider.post(COMPLETIONS_PATH).respond(status)
    with pytest.raises(Exception) as excinfo:
        await ask(llm_settings)
    assert TEST_LLM_API_KEY not in str(excinfo.value)


async def test_the_prompt_is_never_logged(
    provider: respx.MockRouter, llm_settings: Settings, log_buffer: Any
):
    # The prompt contains the ticket's text, which is somebody's content and
    # may contain anything. ADR-009's rule applies to logs as well as rows.
    provider.post(COMPLETIONS_PATH).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UpstreamTimeoutError):
        await llm_client.generate(
            system="s", user="SECRET-TICKET-CONTENT-12345", settings=llm_settings
        )
    assert "SECRET-TICKET-CONTENT-12345" not in log_buffer.getvalue()

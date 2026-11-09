"""HTTP gateway to an OpenAI-compatible chat-completions endpoint.

The only code in ``app/`` that talks to a model. Everything else asks this
module, so adding a second provider is a change here and nowhere else
(ADR-023).

It is a transport, not a policy. It maps HTTP outcomes onto this project's
error types and returns what the provider said, including a truncated reply and
the token counts. Deciding whether a reply is *acceptable* belongs to the
service that asked for it, the same division as ``modules/jira/client.py``
versus ``modules/jira/service.py``.

The default provider is GitHub Models, reached with a GitHub fine-grained
token scoped ``models:read``. The key is read from ``Settings`` at call time
and never logged, never returned, and never put in an error message.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import (
    RateLimitedError,
    ServiceUnavailableError,
    UpstreamError,
    UpstreamTimeoutError,
)

_logger = logging.getLogger("app.llm.client")

_COMPLETIONS_PATH: Final = "/chat/completions"

MAX_RESPONSE_BYTES: Final = 2_000_000
"""Refuse a reply larger than this before parsing it.

``max_tokens`` bounds a *well-behaved* provider. This bounds a misbehaving or
misconfigured one, which is the case a limit is for.
"""

_RETRYABLE_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

_BACKOFF_BASE_SECONDS: Final = 0.5


@dataclass(frozen=True, slots=True)
class Completion:
    """What the model returned.

    Attributes:
        text: The reply's content. Never ``None``; an empty reply is returned
            as an empty string so the caller handles one case, not two.
        model: The model the provider says answered. Recorded rather than
            assumed, because a gateway may route elsewhere and because
            ``LLM_MODEL`` may name an alias.
        prompt_tokens: Tokens charged for the prompt, or ``None`` if the
            provider did not say.
        completion_tokens: Tokens charged for the reply, or ``None``.
        finish_reason: Provider's reason for stopping. ``"length"`` means the
            reply was **cut off** and is incomplete; the caller must check
            this, because a truncated requirements document is not a short one.
    """

    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None

    @property
    def was_truncated(self) -> bool:
        """True when the provider stopped because it hit the token ceiling."""
        return self.finish_reason == "length"


@lru_cache(maxsize=1)
def get_http_client() -> httpx.AsyncClient:
    """Return the process-wide HTTP client for the model provider.

    One client, not one per request: an ``AsyncClient`` owns a connection pool,
    and against a remote API the TLS handshake is most of the latency. Lazy for
    the same reason the database engine is — importing this module must not
    open a socket, and must not require a key that may not be set.
    """
    settings = get_settings()
    key = settings.llm_api_key
    _logger.info("creating llm http client", extra={"llm_base_url": settings.llm_base_url})
    return httpx.AsyncClient(
        base_url=settings.llm_base_url,
        # The credential lives on the client, so no call site handles it.
        headers={
            "Authorization": f"Bearer {key.get_secret_value() if key else ''}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(settings.llm_timeout_seconds),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


async def close_http_client() -> None:
    """Close the pooled connections. Called on application shutdown.

    Checks the cache before calling ``get_http_client()``, because building a
    client purely in order to close it would read settings during shutdown and
    log a line saying it was created.
    """
    if get_http_client.cache_info().currsize == 0:
        return
    await get_http_client().aclose()
    _logger.info("llm http client closed")


def reset_http_client_cache() -> None:
    """Drop the cached client, for tests. Call ``close_http_client()`` first."""
    get_http_client.cache_clear()


async def generate(
    *,
    system: str,
    user: str,
    max_tokens: int | None = None,
    settings: Settings | None = None,
) -> Completion:
    """Ask the model to complete one prompt, retrying transient failures.

    Args:
        system: Instructions. Sent as the ``system`` role.
        user: The content to work on. Sent as the ``user`` role.
        max_tokens: Ceiling on the reply. Defaults to ``LLM_MAX_TOKENS``.
        settings: Override, for tests.

    Returns:
        The completion, which may be empty or truncated. Check
        ``was_truncated`` before using the text.

    Raises:
        ServiceUnavailableError: Generation is not configured. Raised before
            any network call, with a message naming what is missing, because
            the alternative is a 401 from the provider that reads like their
            fault.
        RateLimitedError: The provider rate limited us and retries did not
            clear it. Distinct from ``UpstreamError`` on purpose: the fix is a
            quota decision, not a bug hunt (ADR-023).
        UpstreamTimeoutError: No answer within ``LLM_TIMEOUT_SECONDS``.
        UpstreamError: Anything else — a rejected key, an unknown model, a
            malformed reply, an unreachable host.
    """
    settings = settings or get_settings()
    if not settings.generation_available:
        missing = [
            name
            for name, value in (
                ("LLM_MODEL", settings.llm_model),
                ("LLM_API_KEY", settings.llm_api_key),
            )
            if not value
        ]
        raise ServiceUnavailableError(
            "Document generation is not configured on this server. "
            f"Missing: {', '.join(missing)}. "
            "List available models with GET https://models.github.ai/catalog/models"
        )

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    client = get_http_client()
    attempts = settings.llm_max_retries + 1
    last_error: Exception | None = None
    last_cause: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = await client.post(_COMPLETIONS_PATH, json=payload)
        except httpx.TimeoutException as exc:
            # No prompt text in the log. It is the ticket's content.
            _logger.warning("llm request timed out", extra={"attempt": attempt})
            last_error = UpstreamTimeoutError("The model did not respond in time. Try again.")
            last_cause = exc
        except httpx.HTTPError as exc:
            _logger.warning(
                "llm request failed",
                extra={"attempt": attempt, "error": type(exc).__name__},
            )
            last_error = UpstreamError("Could not reach the model provider.")
            last_cause = exc
        else:
            if response.status_code not in _RETRYABLE_STATUSES:
                return _parse_completion(response, settings)
            _logger.warning(
                "llm returned a retryable status",
                extra={"attempt": attempt, "http_status": response.status_code},
            )
            last_error = _error_for_status(response.status_code)

        if attempt < attempts:
            # Fixed exponential backoff, no jitter. One process generating one
            # document at a time does not need jitter to avoid a thundering
            # herd, and determinism makes the retry path testable.
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    # ``from last_cause`` keeps the original httpx exception in the chain for a
    # traceback, while the raised error carries the caller-facing message. The
    # cause is None when every attempt returned a retryable status rather than
    # failing at the socket.
    raise (
        last_error if last_error else UpstreamError("The model provider could not be reached.")
    ) from last_cause


def _error_for_status(status: int) -> Exception:
    """Map a retryable status onto the error raised once retries run out."""
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return RateLimitedError(
            "The model provider's rate limit was reached. "
            "GitHub Models' free tier is rate limited per model; wait and retry, "
            "or raise the quota."
        )
    return UpstreamError("The model provider reported an error. Try again.")


def _parse_completion(response: httpx.Response, settings: Settings) -> Completion:
    """Turn a non-retryable response into a ``Completion`` or the right error."""
    status = response.status_code

    if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        # Our credential, not the user's problem, so the message stays generic
        # and names the variable an operator would fix.
        _logger.error("llm rejected our credential", extra={"http_status": status})
        raise UpstreamError(
            "The model provider rejected this server's credentials. "
            "Check LLM_API_KEY has the models:read scope."
        )

    if status == httpx.codes.NOT_FOUND:
        # On an OpenAI-compatible endpoint this nearly always means the model
        # name is wrong, not that the path is.
        _logger.error("llm returned 404", extra={"llm_model": settings.llm_model})
        raise UpstreamError(
            f"The model provider does not recognise the model '{settings.llm_model}'. "
            "List available models with GET https://models.github.ai/catalog/models"
        )

    if status >= httpx.codes.BAD_REQUEST:
        _logger.error("llm rejected the request", extra={"http_status": status})
        raise UpstreamError("The model provider rejected the request.")

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise UpstreamError("The model returned a response too large to process.")

    try:
        body = response.json()
    except ValueError as exc:
        raise UpstreamError("The model returned a response that could not be parsed.") from exc

    if not isinstance(body, dict):
        raise UpstreamError("The model returned an unexpected response shape.")

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise UpstreamError("The model returned no completion.")

    first = choices[0]
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    finish_reason = first.get("finish_reason")

    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return Completion(
        # An absent or non-string content becomes "", so callers handle one
        # empty case rather than None and "" separately.
        text=content if isinstance(content, str) else "",
        model=(
            body.get("model") if isinstance(body.get("model"), str) else (settings.llm_model or "")
        ),
        prompt_tokens=_as_int(usage.get("prompt_tokens")),
        completion_tokens=_as_int(usage.get("completion_tokens")),
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
    )


def _as_int(value: object) -> int | None:
    """Return a non-negative int, or ``None`` for anything else.

    Token counts are advisory and providers differ on whether they send them,
    so an odd value is dropped rather than raising: a missing count must not
    fail a generation that otherwise worked.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

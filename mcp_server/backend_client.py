"""HTTP client for the FastAPI backend.

The only thing this process talks to. It holds no database credentials and no
Jira credentials; everything it knows, it asks the backend for. See
``mcp_server/config.py`` for why that separation is enforced by the settings
class rather than by discipline.

Errors are translated into messages a language model can act on. That is the
real job here: a Copilot Chat user sees whatever this returns, so "you do not
have access to project DEF" is useful and "502 Bad Gateway" is not.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_server.config import McpSettings

_logger = logging.getLogger("mcp.backend")

#: Bounds what a misbehaving backend can make this process buffer.
MAX_RESPONSE_BYTES = 2_000_000


class BackendError(Exception):
    """The backend could not serve the request.

    The message is intended for the end user via the language model, so it
    says what to do about it where that is knowable.
    """


def build_client(settings: McpSettings) -> httpx.AsyncClient:
    """Create the HTTP client for the backend.

    One client for the process: it owns a connection pool, and the backend is
    called on every tool invocation.

    The service token is set as a default header here, so no call site handles
    it. The actor header is **not** a default — it changes per call, and a
    default would be a bug waiting to attribute one user's request to another.
    """
    return httpx.AsyncClient(
        base_url=settings.backend_base_url,
        timeout=httpx.Timeout(settings.backend_timeout_seconds),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={
            "Authorization": f"Bearer {settings.internal_api_token.get_secret_value()}",
            "Accept": "application/json",
        },
        follow_redirects=False,
    )


def _envelope_error(payload: object) -> tuple[str | None, str | None]:
    """Pull ``error.code`` and ``error.message`` out of a response envelope."""
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


async def get_issue(
    client: httpx.AsyncClient, issue_key: str, actor_email: str, request_id: str
) -> dict[str, Any]:
    """Fetch one issue from the backend on behalf of ``actor_email``.

    Args:
        client: The pooled client from :func:`build_client`.
        issue_key: Already validated by ``mcp_server.schemas``.
        actor_email: The resolved caller. Sent as ``X-Actor-Email``.
        request_id: Correlation id, sent as ``X-Request-ID`` so this call and
            the backend's log lines and audit rows share one identifier. The
            backend sanitises it before use.

    Returns:
        The ``data`` member of the response envelope.

    Raises:
        BackendError: Any non-200 outcome, with a message for the end user.
    """
    try:
        response = await client.get(
            f"/api/v1/issues/{issue_key}",
            headers={"X-Actor-Email": actor_email, "X-Request-ID": request_id},
        )
    except httpx.TimeoutException as exc:
        _logger.warning("backend timed out", extra={"issue_key": issue_key})
        raise BackendError(
            "The QA platform did not respond in time. Try again in a moment."
        ) from exc
    except httpx.HTTPError as exc:
        _logger.warning(
            "backend unreachable",
            extra={"issue_key": issue_key, "error": type(exc).__name__},
        )
        raise BackendError(
            "The QA platform is unreachable. Check that the backend is running."
        ) from exc

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise BackendError("The QA platform returned a response that was too large.")

    try:
        payload = response.json()
    except ValueError as exc:
        _logger.error("backend returned non-JSON", extra={"http_status": response.status_code})
        raise BackendError("The QA platform returned an unreadable response.") from exc

    if response.status_code == httpx.codes.OK:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise BackendError("The QA platform returned an unexpected response shape.")
        return data

    code, message = _envelope_error(payload)
    _logger.warning(
        "backend refused the request",
        extra={
            "issue_key": issue_key,
            "http_status": response.status_code,
            "error_code": code or "unknown",
        },
    )
    raise BackendError(_user_message(response.status_code, code, message, issue_key))


def _user_message(status: int, code: str | None, message: str | None, issue_key: str) -> str:
    """Turn a backend refusal into something a user can act on.

    The backend's own message is passed through where it is already written for
    an end user, which is the case for 403 and 404. For everything else a fixed
    message is used, because the backend's text for an internal failure is
    deliberately generic and repeating it tells the user nothing.
    """
    if status == httpx.codes.NOT_FOUND:
        # Covers both "no such issue" and "you have no access to that project".
        # The backend does not distinguish them, by design (ADR-010), and this
        # message must not appear to either.
        return (
            f"{issue_key} was not found, or you do not have access to its project. "
            "Ask a QA lead to grant you access if you believe it exists."
        )
    if status == httpx.codes.FORBIDDEN:
        return message or "Your account cannot use the QA platform."
    if status == httpx.codes.UNAUTHORIZED:
        # The end user cannot fix this; it is this server's configuration.
        return "This MCP server is not authorised to call the QA platform."
    # UNPROCESSABLE_ENTITY, not UNPROCESSABLE_CONTENT: httpx keeps the old
    # name for 422 while Starlette has renamed it. Getting this wrong raises
    # AttributeError here rather than failing at import, so it only shows up
    # on the code paths that reach this line.
    if status == httpx.codes.UNPROCESSABLE_ENTITY:
        return f"{issue_key} is not a valid Jira issue key."
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return "Jira is rate limiting the QA platform. Try again shortly."
    if status == httpx.codes.GATEWAY_TIMEOUT:
        return "Jira did not respond in time. Try again in a moment."
    if code == "upstream_error":
        return "The QA platform could not reach Jira. A QA lead should check its configuration."
    return "The QA platform could not serve this request."


async def post_audit(
    client: httpx.AsyncClient,
    *,
    actor_email: str,
    request_id: str,
    action: str,
    outcome: str,
    resource_type: str | None,
    resource_id: str | None,
    detail: dict[str, str | int | float | bool | None],
) -> None:
    """Append one audit record through the backend.

    Raises:
        BackendError: The record could not be written.
    """
    response = await client.post(
        "/api/v1/audit",
        headers={"X-Actor-Email": actor_email, "X-Request-ID": request_id},
        json={
            "action": action,
            "outcome": outcome,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "detail": detail,
        },
    )
    if response.status_code != httpx.codes.CREATED:
        code, _ = _envelope_error(response.json() if response.content else None)
        raise BackendError(f"audit write refused with {response.status_code} ({code or 'unknown'})")

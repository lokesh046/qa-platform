"""HTTP gateway to the Jira Cloud REST API.

Separate from ``repository.py`` on purpose. A repository talks to our database;
this talks to somebody else's service, and the failure modes have nothing in
common. Keeping them apart means ``service.py`` reads as "ask the cache, else
ask Jira" rather than as two kinds of query that happen to look alike.

Authentication is HTTP Basic with the service account's email and API token,
per ADR-002. The token is read from ``Settings`` at call time and never logged,
never returned, and never put in an error message.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import (
    NotFoundError,
    RateLimitedError,
    UpstreamError,
    UpstreamTimeoutError,
)

_logger = logging.getLogger("app.jira.client")

#: Jira Cloud REST API v3. v3 is what returns rich text as ADF; v2 returns
#: wiki markup, which would need a different parser.
_ISSUE_PATH = "/rest/api/3/issue/{issue_key}"

#: Only the fields the platform stores. Asking for everything would pull
#: comments, attachments, worklogs and change history across the wire, all of
#: which ADR-009 says we must not keep. Not requesting them is the cheapest
#: way to guarantee we never accidentally store them.
_BASE_FIELDS = ("summary", "description", "issuetype", "status", "updated")

#: Cap on a Jira response body. A normal issue is a few kilobytes. This bounds
#: what a hostile or broken upstream can make us buffer before the ADF
#: parser's own limits apply.
MAX_RESPONSE_BYTES = 2_000_000


@lru_cache(maxsize=1)
def get_http_client() -> httpx.AsyncClient:
    """Return the process-wide HTTP client for Jira, building it on first call.

    One client, not one per request. An ``AsyncClient`` owns a connection pool,
    and creating one per call throws away connection reuse and the TLS
    handshake with it — which against a remote API is most of the latency.

    Lazy for the same reason the database engine is: importing this module must
    not open a socket.
    """
    settings = get_settings()
    _logger.info("creating jira http client", extra={"jira_base_url": settings.jira_base_url})
    return httpx.AsyncClient(
        base_url=settings.jira_base_url,
        # Credentials live on the client, so no call site handles the token.
        auth=httpx.BasicAuth(
            settings.jira_service_email,
            settings.jira_api_token.get_secret_value(),
        ),
        timeout=httpx.Timeout(settings.jira_timeout_seconds),
        # Bounded so a Jira slowdown cannot open unlimited sockets from here.
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={"Accept": "application/json"},
        # Jira answers an unauthenticated request with a redirect to its login
        # page. Following it would turn a 401 into a confusing 200 of HTML.
        follow_redirects=False,
    )


async def close_http_client() -> None:
    """Close the pooled connections. Called on application shutdown."""
    if get_http_client.cache_info().currsize == 0:
        return
    await get_http_client().aclose()
    _logger.info("jira http client closed")


def reset_http_client_cache() -> None:
    """Drop the cached client, for tests. Call ``close_http_client()`` first."""
    get_http_client.cache_clear()


def _requested_fields(settings: Settings) -> str:
    """Build the ``fields`` query value, including the AC field when set."""
    fields = list(_BASE_FIELDS)
    if settings.jira_acceptance_criteria_field:
        fields.append(settings.jira_acceptance_criteria_field)
    return ",".join(fields)


async def fetch_issue(issue_key: str, settings: Settings | None = None) -> dict[str, Any]:
    """Fetch one issue from Jira and return its raw JSON.

    ``issue_key`` must already be validated; this function does not check its
    shape. The caller is ``service.py``, which validates first, and the router
    rejects a malformed key before that.

    Raises:
        NotFoundError: Jira answered 404. **This also covers "exists but the
            service account cannot see it"** — Jira deliberately returns 404
            rather than 403 for an issue outside the caller's permissions, so
            the two are indistinguishable from here. Treating it as "not
            found" is both the only option and the right answer for the API.
        UpstreamError: Jira rejected our credentials, refused the request, or
            failed. All of these are our misconfiguration or Jira's problem,
            never the end user's, so the message stays generic and the detail
            goes to the log.
        UpstreamTimeoutError: Jira did not answer within the timeout.
        RateLimitedError: Jira applied a rate limit.
    """
    settings = settings or get_settings()
    client = get_http_client()
    path = _ISSUE_PATH.format(issue_key=issue_key)

    try:
        response = await client.get(path, params={"fields": _requested_fields(settings)})
    except httpx.TimeoutException as exc:
        # No URL in the message: it is useless to the caller and the path
        # contains the issue key, which belongs in the audit trail, not here.
        _logger.warning("jira request timed out", extra={"issue_key": issue_key})
        raise UpstreamTimeoutError("Jira did not respond in time. Try again.") from exc
    except httpx.HTTPError as exc:
        # Covers DNS failure, connection refused, TLS errors. str(exc) can
        # contain the host but never the token, since httpx renders auth
        # headers as a separate object.
        _logger.warning(
            "jira request failed", extra={"issue_key": issue_key, "error": type(exc).__name__}
        )
        raise UpstreamError("Could not reach Jira.") from exc

    return _handle_response(response, issue_key)


def _handle_response(response: httpx.Response, issue_key: str) -> dict[str, Any]:
    """Turn a Jira HTTP response into parsed JSON or the right AppError."""
    status = response.status_code

    if status == httpx.codes.NOT_FOUND:
        # Expected and common. Logged at INFO, not WARNING: a typo in an issue
        # key is a user error, not an incident.
        _logger.info("jira issue not found", extra={"issue_key": issue_key})
        raise NotFoundError(f"Issue {issue_key} was not found.")

    if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        # The service account's credentials are wrong, expired, or lack
        # permission. An operator needs to know immediately; the caller must
        # not be told which, because the distinction describes our
        # configuration and not their request.
        _logger.error(
            "jira rejected the service account; check JIRA_SERVICE_EMAIL and "
            "JIRA_API_TOKEN, and that the account can see this project",
            extra={"issue_key": issue_key, "jira_status": status},
        )
        raise UpstreamError("Jira rejected this service's credentials.")

    if status == httpx.codes.TOO_MANY_REQUESTS:
        retry_after = response.headers.get("Retry-After")
        _logger.warning(
            "jira rate limited this service",
            extra={"issue_key": issue_key, "retry_after": retry_after},
        )
        raise RateLimitedError(
            "Jira is rate limiting this service. Try again shortly.",
            details={"retry_after": retry_after} if retry_after else None,
        )

    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        _logger.error("jira server error", extra={"issue_key": issue_key, "jira_status": status})
        raise UpstreamError("Jira reported an internal error.")

    if status != httpx.codes.OK:
        _logger.error(
            "unexpected jira status", extra={"issue_key": issue_key, "jira_status": status}
        )
        raise UpstreamError("Jira returned an unexpected response.")

    if len(response.content) > MAX_RESPONSE_BYTES:
        _logger.error(
            "jira response too large",
            extra={"issue_key": issue_key, "response_bytes": len(response.content)},
        )
        raise UpstreamError("Jira returned a response that was too large to process.")

    try:
        payload = response.json()
    except ValueError as exc:
        # Almost always an HTML error or login page with a 200 status.
        _logger.error("jira returned non-JSON", extra={"issue_key": issue_key})
        raise UpstreamError("Jira returned a response that could not be parsed.") from exc

    if not isinstance(payload, dict):
        _logger.error(
            "jira returned unexpected JSON shape",
            extra={"issue_key": issue_key, "json_type": type(payload).__name__},
        )
        raise UpstreamError("Jira returned an unexpected response shape.")

    return payload

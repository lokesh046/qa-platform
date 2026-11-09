"""Recording what the gatekeeper did.

Every tool call writes one record, whatever the outcome. The record goes
through the backend's ``POST /api/v1/audit``, not to the database, because this
process holds no database credentials — see ``mcp_server/config.py``.

This is the second of two rows per tool call. The backend writes its own
``source=api`` row for the data access; this one is ``source=mcp`` and records
the tool invocation, including the validation failures that never reach the
backend at all. Anyone counting reads must filter on ``source`` or they will
double-count. See ADR-013.
"""

from __future__ import annotations

import logging

import httpx

from mcp_server.backend_client import BackendError, post_audit

_logger = logging.getLogger("mcp.audit")

#: The action recorded for the ``get_jira_issue`` tool. The ``mcp.`` prefix is
#: required by the backend, which refuses any other namespace so that this
#: process cannot forge a row that looks like the backend's own.
GET_JIRA_ISSUE_ACTION = "mcp.get_jira_issue"

_RESOURCE_TYPE = "jira_issue"

ALLOWED = "allowed"
DENIED = "denied"
ERROR = "error"


async def record(
    client: httpx.AsyncClient,
    *,
    actor_email: str,
    request_id: str,
    outcome: str,
    issue_key: str | None,
    reason: str | None = None,
    **detail: str | int | float | bool | None,
) -> None:
    """Append one audit record for a tool call.

    Failures propagate. A gatekeeper that cannot record what it did should not
    quietly carry on; ADR-013 has the argument, and this matches the backend's
    own behaviour so the two layers fail the same way.

    Args:
        client: The pooled backend client.
        actor_email: Who the call was for.
        request_id: Correlates this row with the backend's own row and with
            both processes' log lines.
        outcome: :data:`ALLOWED`, :data:`DENIED` or :data:`ERROR`.
        issue_key: Recorded as the resource id. ``None`` when the argument was
            too malformed to be worth storing.
        reason: Short machine-readable cause, for denials and errors.
        **detail: Extra scalar context. Never a credential or a payload.
    """
    payload: dict[str, str | int | float | bool | None] = dict(detail)
    if reason is not None:
        payload["reason"] = reason
    try:
        await post_audit(
            client,
            actor_email=actor_email,
            request_id=request_id,
            action=GET_JIRA_ISSUE_ACTION,
            outcome=outcome,
            resource_type=_RESOURCE_TYPE,
            resource_id=issue_key,
            detail=payload,
        )
    except (BackendError, httpx.HTTPError):
        _logger.exception(
            "failed to write audit record",
            extra={"outcome": outcome, "issue_key": issue_key},
        )
        raise

"""The MCP server process: one tool, two transports.

Run it directly:

    python -m mcp_server.server

``MCP_TRANSPORT`` chooses the transport. ``stdio`` is for Copilot Chat, which
launches this process itself; ``http`` is for the container. ADR-004 covers the
choice and ADR-014 the different trust models.

A note on the SDK: the brief names FastMCP, which was the high-level server API
in ``mcp`` 1.x. In ``mcp`` 2.x it is called ``MCPServer``. This file uses the
current name. See the Step 7 log.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Annotated, Any

import anyio
import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from app import __version__

# A pure formatter. Importing this module does not construct Settings and so does not
# require DATABASE_URL or JIRA_API_TOKEN; see mcp_server/config.py on the boundary.
from app.core.config import format_validation_error
from app.core.logging import configure_logging
from mcp_server import audit
from mcp_server.auth import CallerRefusedError, check_startup_identity, resolve_actor
from mcp_server.backend_client import BackendError, build_client, get_issue
from mcp_server.config import McpSettings, load_settings
from mcp_server.schemas import GetJiraIssueArgs

_logger = logging.getLogger("mcp.server")

#: Fields of the backend's IssueRead that the tool returns. The backend already
#: returns only a small projection of a Jira issue (ADR-009); this narrows it
#: further to what the brief asks for plus the context a reader needs to trust
#: the answer.
_RETURNED_FIELDS = (
    "issue_key",
    "project_key",
    "summary",
    "acceptance_criteria",
    "issue_type",
    "status",
    "jira_updated_at",
    "from_cache",
)


def _tool_description() -> str:
    """The text the language model reads when deciding whether to call this.

    Written for a model, not a developer. It states what comes back, what does
    not, and what the argument must look like, because a model that guesses an
    argument format produces a confident error message instead of an answer.
    """
    return (
        "Read one Jira issue from the internal QA platform and return its summary "
        "and acceptance criteria.\n\n"
        "Use this when the user names a Jira issue key such as ABC-123, or asks "
        "what an issue says, what its acceptance criteria are, or what needs "
        "testing for it.\n\n"
        "The issue_key argument must be an uppercase project key, a hyphen, then "
        "digits: ABC-123. Lowercase keys, URLs and issue titles are rejected. If "
        "the user gives a Jira URL, extract the key from it first.\n\n"
        "Access is per project and is checked against the caller's own "
        "permissions, not the platform's. A 'not found' result can mean the issue "
        "does not exist or that the caller has no access to that project; the two "
        "are not distinguished. Do not retry on that result.\n\n"
        "Returns the summary, acceptance criteria, issue type, status and project "
        "key. It does not return comments, attachments, assignees or reporters."
    )


def _project_key_of(issue_key: str) -> str:
    """The project key embedded in an issue key, for audit detail."""
    return issue_key.split("-", 1)[0]


def _first_validation_rule(exc: ValidationError) -> str:
    """Name the rule that rejected an argument, for the audit record.

    The failing *value* is deliberately not recorded. It is model-supplied
    free text, and ADR-009 forbids storing that verbatim.
    """
    errors = exc.errors()
    if not errors:
        return "invalid_arguments"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
    return f"{location}:{first.get('type', 'invalid')}"


def build_server(settings: McpSettings, client: httpx.AsyncClient) -> MCPServer:
    """Create the server and register its tools.

    Takes the client rather than building one so that the process owns the
    lifecycle and a test can supply its own.
    """
    server = MCPServer(
        name="qa-platform",
        title="AI QA Automation Platform",
        version=__version__,
        instructions=(
            "Read-only access to Jira issues through the internal QA platform. "
            "Every call is authorised against the individual user's project "
            "access and recorded in an audit trail."
        ),
    )

    @server.tool(name="get_jira_issue", description=_tool_description())
    async def get_jira_issue(
        issue_key: Annotated[
            str,
            Field(
                description=(
                    "A Jira issue key: uppercase project key, a hyphen, then digits. "
                    "For example ABC-123."
                ),
                examples=["ABC-123", "QA-7"],
            ),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        """Return one issue's summary and acceptance criteria.

        The order here is the same shape as the backend's: identify the caller,
        validate the argument, then act — and record the outcome either way.

        Resolving the actor comes first because an audit record without an
        actor is close to useless, and because on http the token check lives
        there. A caller who cannot be identified is refused before their
        arguments are even looked at.

        **Every failure here raises ToolError.** That is the only exception
        class whose message the SDK forwards to the model; anything else
        becomes a bare "Error executing tool get_jira_issue", logged at ERROR
        with a traceback as though the server had crashed. Using ``ValueError``
        here silently discarded every message below — which was the whole
        point of writing them — and misfiled expected refusals as crashes.

        **Why the regex is not on the annotation.** It would be easy to put
        ``pattern=ISSUE_KEY_PATTERN`` in the ``Field`` above and let the SDK
        reject a bad key before this function runs. That was tried and
        rejected: the SDK's rejection happens outside this handler, so no audit
        record would be written, and a run of malformed calls from one actor is
        exactly the kind of probing the audit trail exists to show. Validation
        therefore happens below, via ``GetJiraIssueArgs``, where the outcome
        can be recorded. The description and examples still tell the model the
        format, so the cost is only a slightly thinner JSON schema.
        """
        # One id per tool call, forwarded to the backend as X-Request-ID so
        # this call, the backend's log lines and both audit rows share it.
        request_id = uuid.uuid4().hex

        try:
            actor_email = resolve_actor(ctx.headers, settings)
        except CallerRefusedError as exc:
            # Not audited: there is no actor to attribute a record to, and the
            # backend would reject a row without one.
            _logger.warning("tool call refused before identity", extra={"reason": str(exc)})
            raise ToolError(str(exc)) from exc

        try:
            args = GetJiraIssueArgs(issue_key=issue_key)
        except ValidationError as exc:
            rule = _first_validation_rule(exc)
            await audit.record(
                client,
                actor_email=actor_email,
                request_id=request_id,
                outcome=audit.DENIED,
                issue_key=None,
                reason=rule,
            )
            _logger.info("tool argument rejected", extra={"rule": rule})
            raise ToolError(
                f"{issue_key!r} is not a valid Jira issue key. Expected an uppercase "
                "project key, a hyphen, then digits, for example ABC-123."
            ) from exc

        try:
            data = await get_issue(client, args.issue_key, actor_email, request_id)
        except BackendError as exc:
            await audit.record(
                client,
                actor_email=actor_email,
                request_id=request_id,
                outcome=audit.ERROR,
                issue_key=args.issue_key,
                reason="backend_refused",
                project_key=_project_key_of(args.issue_key),
            )
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            # A bug in this process must still leave a record. This branch is
            # not hypothetical: an AttributeError inside the backend client's
            # message builder once escaped as itself rather than as a
            # BackendError, so the branch above never ran and the call produced
            # no audit row at all. The trail must not depend on the rest of
            # this function being correct.
            #
            # Re-raised unchanged, so the SDK still treats it as a crash and
            # logs the traceback at ERROR. The model gets a generic message,
            # which is right: an internal fault is not something it can act on.
            _logger.exception("unexpected failure in get_jira_issue")
            await audit.record(
                client,
                actor_email=actor_email,
                request_id=request_id,
                outcome=audit.ERROR,
                issue_key=args.issue_key,
                reason="internal_error",
                error_type=type(exc).__name__,
            )
            raise

        await audit.record(
            client,
            actor_email=actor_email,
            request_id=request_id,
            outcome=audit.ALLOWED,
            issue_key=args.issue_key,
            project_key=_project_key_of(args.issue_key),
            from_cache=bool(data.get("from_cache")),
        )
        return {field: data.get(field) for field in _RETURNED_FIELDS}

    return server


async def _serve(settings: McpSettings) -> None:
    """Build the client and server, then run the chosen transport."""
    client = build_client(settings)
    server = build_server(settings, client)
    try:
        if settings.is_http:
            _logger.info(
                "serving mcp over http",
                extra={"host": settings.mcp_host, "port": settings.mcp_port},
            )
            await server.run_streamable_http_async(host=settings.mcp_host, port=settings.mcp_port)
        else:
            # Nothing may be written to stdout on stdio: stdout *is* the
            # protocol channel. configure_logging sends records to stdout, so
            # main() redirects them to stderr before this point.
            _logger.info("serving mcp over stdio")
            await server.run_stdio_async()
    finally:
        await client.aclose()


def main() -> int:
    """Configure, validate, and run. Returns a process exit code."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        # format_validation_error, not str(exc): the latter appends
        # input_value=... for every rejected field, which for this model means
        # printing INTERNAL_API_TOKEN to stderr. See ADR-016.
        sys.stderr.write(f"mcp server configuration error:\n{format_validation_error(exc)}\n")
        return 2
    except Exception as exc:  # operator-facing startup message, not a traceback
        sys.stderr.write(f"mcp server configuration error:\n{type(exc).__name__}: {exc}\n")
        return 2

    # On stdio, stdout carries the MCP protocol, so every log record must go to
    # stderr or the first log line corrupts the stream. This is the single
    # easiest way to break an MCP server and it fails confusingly.
    configure_logging(
        level=settings.log_level,
        service="qa-platform-mcp",
        stream=sys.stderr,
    )

    try:
        check_startup_identity(settings)
    except CallerRefusedError as exc:
        _logger.error("refusing to start", extra={"reason": str(exc)})
        sys.stderr.write(f"mcp server cannot start:\n{exc}\n")
        return 2

    _logger.info(
        "mcp server starting",
        extra={
            "version": __version__,
            "transport": settings.mcp_transport,
            "backend_base_url": settings.backend_base_url,
            "actor_source": "request header" if settings.is_http else "QA_ACTOR_EMAIL",
        },
    )

    try:
        anyio.run(_serve, settings)
    except KeyboardInterrupt:
        _logger.info("mcp server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

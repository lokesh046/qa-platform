"""JSON logging, and the request_id that ties a request's log lines together.

Two pieces work together here:

* ``configure_logging`` replaces the root handler with one that writes a single
  JSON object per line to stdout. Container runtimes collect stdout, so there
  is no log file to rotate and no path to configure.
* ``RequestIdMiddleware`` assigns an id to every request, publishes it in a
  ``ContextVar``, and echoes it in the ``X-Request-ID`` response header. The
  formatter reads that ContextVar, so every log line emitted while handling a
  request carries the id without anyone passing it around.

The id is the thread that makes a production log searchable: a user reports a
failure, quotes the id from the error response, and one query returns every
line for that request.

Nothing here logs a request body, a response body, or a header value. See
``REDACTION`` below.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Final, TextIO

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------
# REDACTION
#
# Request and response bodies are never logged: they carry Jira content and,
# later, test data. Headers are never logged: Authorization holds the service
# token. Query strings are never logged: they are the easiest place for a
# secret to end up by accident, even if none of our endpoints put one there.
#
# What is logged for each request: method, path, status, duration. That is
# enough to find a slow or failing endpoint, and not enough to leak anything.
# ---------------------------------------------------------------------------

_REQUEST_ID_HEADER: Final = "x-request-id"
_MAX_INBOUND_REQUEST_ID_LENGTH: Final = 64

#: Set per request by :class:`RequestIdMiddleware`; read by :class:`JsonFormatter`.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Attributes every ``LogRecord`` carries. Anything outside this set was passed
#: by the caller as ``extra=`` and is merged into the JSON output.
_STANDARD_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def get_request_id() -> str | None:
    """Return the current request's id, or None outside a request."""
    return _request_id.get()


def set_request_id(request_id: str | None) -> None:
    """Set the current request id.

    Used by the middleware, and by background workers that want their log
    lines attributed to the request that enqueued the job.
    """
    _request_id.set(request_id)


def new_request_id() -> str:
    """Generate a fresh request id."""
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as one JSON object on one line.

    Fields are ordered so a human skimming raw output sees time, level and
    message first. Anything passed as ``extra={...}`` is merged in at the top
    level, which is what makes structured queries possible:
    ``logger.info("jira fetch", extra={"issue_key": "ABC-1", "cached": True})``.

    A failure to serialise a value must never lose the log line, so unknown
    types fall back to ``repr`` rather than raising.
    """

    def __init__(self, *, service: str) -> None:
        """Store the service name stamped onto every line."""
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a JSON string."""
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = _request_id.get()
        if request_id is not None:
            payload["request_id"] = request_id

        # Caller-supplied structured fields.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=repr, ensure_ascii=False)

    def formatTime(  # noqa: N802  # overriding a stdlib camelCase method
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,  # noqa: ARG002  # stdlib signature
    ) -> str:
        """Format the record timestamp as UTC ISO-8601 with milliseconds.

        ``datefmt`` is part of the stdlib signature and deliberately ignored:
        the timestamp format is fixed so machine parsers can rely on it.
        """
        utc = time.gmtime(record.created)
        base = time.strftime("%Y-%m-%dT%H:%M:%S", utc)
        return f"{base}.{int(record.msecs):03d}Z"


def configure_logging(
    *,
    level: str = "INFO",
    service: str = "qa-platform",
    stream: TextIO | None = None,
) -> None:
    """Install the JSON handler on the root logger.

    Call once, as early as possible in process startup, before anything else
    logs. Safe to call again: existing handlers are removed first, so a reload
    does not double every line.

    ``stream`` defaults to stdout, which is what a container wants. The MCP
    server passes ``sys.stderr`` instead, and must: on its stdio transport
    stdout *is* the protocol channel, so a single log line written there
    corrupts the stream. That failure is silent and confusing, which is why
    this parameter exists rather than leaving each caller to reach for
    ``logging.StreamHandler``.

    Third-party loggers are folded into the same handler by clearing their own
    handlers and letting them propagate to root. ``uvicorn.access`` is silenced
    entirely because :class:`RequestIdMiddleware` emits a better line, with the
    request id attached and without the query string.
    """
    handler = logging.StreamHandler(stream=stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "fastapi", "httpx", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # httpx logs every outbound request at INFO, including the full URL with
    # its query string. Nothing currently puts a secret in a query parameter,
    # but "never log tokens" is easier to keep as an invariant than as a thing
    # to remember whenever a new upstream call is added. Our own clients log
    # what matters: see app/modules/jira/client.py and app/modules/jira/
    # service.py. Raised to WARNING so genuine transport failures still show.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Our own middleware logs every request, with the request id included.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True


def _sanitise_inbound_request_id(raw: str) -> str | None:
    """Accept a caller-supplied request id only if it is safe to echo.

    An inbound id is attacker-controlled: it goes into our logs and back out in
    a response header. Allowing arbitrary bytes invites log injection through
    embedded newlines and header splitting through control characters. The rule
    is deliberately narrow: printable ASCII, no spaces, no more than 64
    characters. Anything else is discarded and a fresh id is generated.
    """
    candidate = raw.strip()
    if not candidate or len(candidate) > _MAX_INBOUND_REQUEST_ID_LENGTH:
        return None
    if not all(c.isalnum() or c in "-_." for c in candidate):
        return None
    return candidate


class RequestIdMiddleware:
    """Assign a request id, publish it, echo it, and log the request.

    Written as raw ASGI rather than Starlette's ``BaseHTTPMiddleware`` on
    purpose. ``BaseHTTPMiddleware`` runs the downstream app in a separate task,
    which breaks ContextVar propagation in exactly the way that would make the
    request id vanish from log lines, and it buffers streaming responses.
    """

    def __init__(self, app: ASGIApp, *, logger_name: str = "app.request") -> None:
        """Wrap ``app`` and prepare the access logger."""
        self._app = app
        self._logger = logging.getLogger(logger_name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI event stream."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        token = _request_id.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((_REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            # Path only: the query string is never logged (see REDACTION).
            self._logger.info(
                "request",
                extra={
                    "http_method": scope.get("method", ""),
                    "http_path": scope.get("path", ""),
                    "http_status": status_code,
                    "duration_ms": duration_ms,
                },
            )
            _request_id.reset(token)

    @staticmethod
    def _resolve_request_id(scope: Scope) -> str:
        """Reuse a safe inbound ``X-Request-ID``, otherwise generate one."""
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.decode("latin-1").lower() == _REQUEST_ID_HEADER:
                sanitised = _sanitise_inbound_request_id(raw_value.decode("latin-1"))
                if sanitised is not None:
                    return sanitised
                break
        return new_request_id()

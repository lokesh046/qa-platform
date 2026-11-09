"""The standard response envelope and the application error hierarchy.

Every response this API produces, success or failure, has the same three keys::

    {"data": ..., "error": ..., "request_id": "..."}

On success ``error`` is ``null``. On failure ``data`` is ``null`` and ``error``
is ``{"code": ..., "message": ..., "details": ...}``. A client therefore never
has to branch on the shape of a response to find out whether it worked, and
every failure carries the ``request_id`` needed to find the matching log lines.

Raising is the normal way to fail. A service raises :class:`NotFoundError`; the
handler registered by :func:`register_exception_handlers` turns it into the
right status code and envelope. Services never build an HTTP response, and
routers never translate exceptions by hand.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id

_logger = logging.getLogger("app.error")

#: Returned to clients in place of any unexpected exception's message. The real
#: message and traceback go to the log, never to the caller: exception text
#: routinely contains table names, file paths, SQL fragments and occasionally
#: connection strings.
_GENERIC_INTERNAL_MESSAGE = "An internal error occurred. Quote the request_id when reporting it."


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers.

    Clients branch on these, so treat them as API surface: add freely, rename
    never. The HTTP status says how to react in general; the code says exactly
    what happened.
    """

    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


class ErrorBody(BaseModel):
    """The ``error`` member of a failed response."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable explanation, safe to show a user.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context, for example per-field validation errors.",
    )


class ResponseEnvelope[T](BaseModel):
    """The shape of every response from this API.

    Generic in the payload type so that OpenAPI documents the real schema:
    ``ResponseEnvelope[IssueRead]`` renders ``data`` as an ``IssueRead``.
    """

    model_config = ConfigDict(extra="forbid")

    data: T | None = Field(default=None, description="The payload, or null when the call failed.")
    error: ErrorBody | None = Field(default=None, description="The failure, or null on success.")
    request_id: str | None = Field(
        default=None, description="Correlates this response with the server log."
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Base class for every failure this application raises deliberately.

    Carries the HTTP status, the stable error code, and a message written for
    the caller. Subclasses fix the status and code so call sites read as
    ``raise NotFoundError("Issue ABC-1 not found")``.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        """Record a caller-safe message and optional structured context."""
        super().__init__(message)
        self.message = message
        self.details = details

    def to_body(self) -> ErrorBody:
        """Render this error as the ``error`` member of the envelope."""
        return ErrorBody(code=self.code, message=self.message, details=self.details)


class ValidationFailedError(AppError):
    """Input was syntactically valid but unacceptable.

    Named with a suffix to avoid colliding with ``pydantic.ValidationError``,
    which is a different thing and easy to confuse at an import site.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.VALIDATION_ERROR


class UnauthorizedError(AppError):
    """The caller did not present a valid service token."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHORIZED


class ForbiddenError(AppError):
    """The caller is known but is not allowed to do this."""

    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.FORBIDDEN


class NotFoundError(AppError):
    """The requested resource does not exist, or the caller may not see it."""

    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    """The request clashes with the current state of the resource."""

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.CONFLICT


class RateLimitedError(AppError):
    """An upstream or internal rate limit was reached."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.RATE_LIMITED


class UpstreamError(AppError):
    """A dependency such as Jira answered, but not usefully."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = ErrorCode.UPSTREAM_ERROR


class UpstreamTimeoutError(AppError):
    """A dependency such as Jira did not answer in time."""

    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = ErrorCode.UPSTREAM_TIMEOUT


class ServiceUnavailableError(AppError):
    """This service is running but cannot currently serve traffic.

    Raised by ``/ready`` when a dependency it needs is unreachable. Distinct
    from :class:`UpstreamError`: a 502 means a request failed because a
    dependency misbehaved, while a 503 means do not send the request at all
    yet. Load balancers and Kubernetes readiness probes treat the two
    differently, so the distinction is worth keeping.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def success_payload(data: Any) -> dict[str, Any]:
    """Build a successful envelope around ``data``."""
    return {"data": data, "error": None, "request_id": get_request_id()}


def error_payload(
    code: ErrorCode, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a failed envelope."""
    return {
        "data": None,
        "error": {"code": code.value, "message": message, "details": details},
        "request_id": get_request_id(),
    }


def error_response(
    status_code: int, code: ErrorCode, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    """Build a ``JSONResponse`` holding a failed envelope."""
    return JSONResponse(status_code=status_code, content=error_payload(code, message, details))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`AppError` as its declared status and code.

    Logged at WARNING, without a traceback: these are expected outcomes, not
    defects, and a stack trace for every 404 buries the real problems.
    """
    assert isinstance(exc, AppError)  # noqa: S101  # handler is registered for this type only
    _logger.warning(
        "handled error",
        extra={"error_code": exc.code.value, "http_status": exc.status_code},
    )
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render FastAPI's request-validation failure in our envelope.

    FastAPI's default response is a bare ``{"detail": [...]}``, which breaks the
    envelope contract. The field errors are preserved because they are the
    useful part, but the ``input`` member of each is dropped: it echoes the
    caller's value, and for a malformed auth payload that means echoing a
    credential into the response and the log.
    """
    assert isinstance(exc, RequestValidationError)  # noqa: S101  # registered for this type
    fields = [
        {
            "location": list(error.get("loc", ())),
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    _logger.info("request validation failed", extra={"field_error_count": len(fields)})
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        "Request validation failed.",
        {"fields": fields},
    )


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette's own ``HTTPException`` in our envelope.

    Starlette raises these before our code runs, for example on an unmatched
    route (404) or a wrong method (405). Without this handler those few
    responses would be the only ones not using the envelope.
    """
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101  # registered for this type
    code = {
        status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_response(exc.status_code, code, message)


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Log an unexpected exception in full and return a generic 500.

    This is the last line of defence, and it has one job beyond logging: make
    sure the caller learns nothing about our internals. The traceback goes to
    the log with the request id attached; the response carries that same id and
    a fixed message, so support can correlate the two without the client ever
    seeing a table name or a file path.
    """
    _logger.exception("unhandled exception", exc_info=exc)
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        _GENERIC_INTERNAL_MESSAGE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler above to ``app``.

    Called once from ``app/main.py``. Order does not matter: FastAPI dispatches
    on the exception's type, most specific first.
    """
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

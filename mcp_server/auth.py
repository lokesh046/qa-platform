"""Resolving who a tool call is for, and whether it may proceed.

Two transports with genuinely different trust models, which is the whole
content of this module.

**stdio.** Copilot Chat launches this process itself, as the developer, and
speaks to it over the process's own pipes. Nothing else can reach it. The
process boundary *is* the identity, so the actor comes from
``QA_ACTOR_EMAIL`` in the environment.

**http.** One process serves many callers over a socket. A header is
client-supplied input — the MCP SDK says so in as many words: "never treat one
as an identity assertion." So the actor header alone is not enough. A caller
must also present the shared service token, which is what makes the actor
header a claim from a trusted peer rather than from anybody who can reach the
port. Only then is the actor read from the request. ``QA_ACTOR_EMAIL`` is
ignored entirely on http, so a misconfigured container cannot quietly
attribute every call to one person. See ADR-014.
"""

from __future__ import annotations

import hmac
import logging

from pydantic import EmailStr, TypeAdapter, ValidationError

from mcp_server.config import McpSettings

_logger = logging.getLogger("mcp.auth")

_ACTOR_HEADER = "x-actor-email"
_AUTH_HEADER = "authorization"
_BEARER_PREFIX = "bearer "

_email_adapter: TypeAdapter[EmailStr] = TypeAdapter(EmailStr)


class CallerRefusedError(Exception):
    """The call may not proceed.

    Carries a message written for the caller. It says what is missing, never
    what was expected: "token rejected", not "expected a token starting with".
    """


def _header(headers: object, name: str) -> str | None:
    """Read one header case-insensitively from a mapping that may be absent."""
    if headers is None:
        return None
    try:
        items = headers.items()  # type: ignore[union-attr]
    except AttributeError:
        return None
    for key, value in items:
        if str(key).lower() == name and isinstance(value, str):
            return value
    return None


def _verify_token(presented: str | None, settings: McpSettings) -> None:
    """Check the shared service token in constant time.

    ``hmac.compare_digest`` rather than ``==`` so the comparison does not
    return early on the first wrong byte. The same reasoning as
    ``app.core.security.verify_service_token``; the two are deliberately
    identical in approach.
    """
    if presented is None:
        raise CallerRefusedError("This server requires an Authorization header.")
    if not presented.lower().startswith(_BEARER_PREFIX):
        raise CallerRefusedError("Authorization header must be 'Bearer <token>'.")
    token = presented[len(_BEARER_PREFIX) :].strip()
    expected = settings.internal_api_token.get_secret_value()
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        # No part of either token is logged. The length is not logged either:
        # it narrows a guess.
        _logger.warning("mcp caller token rejected")
        raise CallerRefusedError("Token rejected.")


def _parse_email(raw: str) -> str:
    """Validate an email address and normalise it to lowercase."""
    candidate = raw.strip().lower()
    try:
        _email_adapter.validate_python(candidate)
    except ValidationError as exc:
        raise CallerRefusedError("The actor email is not a valid address.") from exc
    return candidate


def resolve_actor(headers: object, settings: McpSettings) -> str:
    """Return the email this call acts for, or refuse it.

    Args:
        headers: ``Context.headers`` from the tool call. ``None`` on stdio.
        settings: The process settings.

    Raises:
        CallerRefusedError: On http, the token is missing or wrong, or the actor
            header is absent or malformed. On stdio, ``QA_ACTOR_EMAIL`` is
            unset or malformed.
    """
    if settings.is_http:
        _verify_token(_header(headers, _AUTH_HEADER), settings)
        raw = _header(headers, _ACTOR_HEADER)
        if not raw:
            raise CallerRefusedError(
                "This server requires an X-Actor-Email header naming the user the request is for."
            )
        return _parse_email(raw)

    if headers is not None:
        # Belt and braces. If a future transport delivers headers while the
        # process believes it is on stdio, the environment identity would be
        # applied to a request from somewhere else.
        _logger.warning("headers present on a stdio transport; refusing")
        raise CallerRefusedError("Unexpected transport for this server's configuration.")

    if settings.qa_actor_email is None:
        raise CallerRefusedError(
            "QA_ACTOR_EMAIL is not set. Set it to your work email so the "
            "platform can check your project access."
        )
    return _parse_email(settings.qa_actor_email)


def check_startup_identity(settings: McpSettings) -> None:
    """Fail at startup if identity cannot work for the chosen transport.

    Called before the server starts listening, because the alternative is a
    process that accepts connections and then refuses every call, or worse,
    attributes them all to the same person.

    Raises:
        CallerRefusedError: stdio without ``QA_ACTOR_EMAIL``.
    """
    if settings.is_http:
        if settings.qa_actor_email is not None:
            # Not fatal, but it means somebody expects it to be used. Saying so
            # is cheaper than the support conversation about why every audit
            # row names one person.
            _logger.warning(
                "QA_ACTOR_EMAIL is set but is ignored on the http transport; "
                "each request must carry its own X-Actor-Email header"
            )
        return

    if settings.qa_actor_email is None:
        raise CallerRefusedError(
            "QA_ACTOR_EMAIL must be set when MCP_TRANSPORT=stdio. On stdio the "
            "process serves one person and there is no per-request identity."
        )

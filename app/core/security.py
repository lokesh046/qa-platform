"""Internal authentication: the service token, and the actor it acts for.

Phase 1 trusts one channel. The MCP server presents a shared service token and
names the human it is acting for; the backend verifies the token and takes the
name on trust *because* the token was valid. ADR-003 explains why, and records
the limitation plainly: anyone holding ``INTERNAL_API_TOKEN`` can claim to be
any user, so the token is a high-value secret and this design is only
acceptable while it stays on trusted machines.

Two rules are load-bearing here:

1. The token is compared with :func:`hmac.compare_digest`, never ``==``. A
   plain comparison returns early on the first differing byte, which leaks the
   token's prefix through response timing.
2. The actor header is read only after the token has been verified. Reading it
   first would let an unauthenticated caller choose whose name ends up in the
   audit log.

Nothing in this module logs a token, not even a truncated one.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Final

from fastapi import Depends, Header
from pydantic import BaseModel, ConfigDict, EmailStr, ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError

_logger = logging.getLogger("app.security")

_BEARER_PREFIX: Final = "bearer "

#: Sent on a 401 so a client knows which scheme to use.
UNAUTHORIZED_HEADERS: Final[dict[str, str]] = {"WWW-Authenticate": "Bearer"}


class Actor(BaseModel):
    """The human a request is being made on behalf of.

    Carries only the identity. Whether that identity may see a particular Jira
    project is a separate question, answered by the ``users`` module against
    ``user_project_access`` in Step 6. Keeping the two apart means
    authentication cannot accidentally imply authorisation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    email: EmailStr

    def __str__(self) -> str:
        """Return the email, which is what belongs in an audit record."""
        return str(self.email)


class _ActorHeader(BaseModel):
    """Internal helper that applies email validation to the raw header value."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


def _extract_bearer_token(authorization: str | None) -> str:
    """Pull the credential out of an ``Authorization: Bearer <token>`` header.

    Raises:
        UnauthorizedError: if the header is absent or not a Bearer header.
    """
    if not authorization:
        raise UnauthorizedError(
            "Missing Authorization header.", details={"expected": "Bearer <token>"}
        )
    if not authorization.lower().startswith(_BEARER_PREFIX):
        raise UnauthorizedError(
            "Authorization header must use the Bearer scheme.",
            details={"expected": "Bearer <token>"},
        )
    return authorization[len(_BEARER_PREFIX) :].strip()


def verify_service_token(presented: str, settings: Settings) -> None:
    """Check a presented token against the configured one, in constant time.

    Raises:
        UnauthorizedError: if the token does not match.
    """
    expected = settings.internal_api_token.get_secret_value()
    # compare_digest needs bytes or ASCII-only str; encode so a non-ASCII
    # token cannot raise TypeError and turn a 401 into a 500.
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        # No token material in the log. The request id is enough to find the
        # caller's other log lines if this needs investigating.
        _logger.warning("service token rejected")
        raise UnauthorizedError("Invalid service token.")


def parse_actor(raw_email: str | None) -> Actor:
    """Validate the ``X-Actor-Email`` header and return the actor.

    The address is lowercased, which is a correctness requirement rather than
    tidiness. ``EmailStr`` normalises the domain but preserves the local part's
    case, so ``QA.One@acme.co`` and ``qa.one@acme.co`` would reach the audit
    trail as two different actors while matching the same user row — and
    ``users.email`` has a CHECK constraint that only permits the lowercase
    form, so nothing is lost by canonicalising here. ``mcp_server.auth`` does
    the same. Found by the Step 8 tests; see ADR-017.

    Raises:
        UnauthorizedError: if the header is missing or is not a valid address.
    """
    if not raw_email:
        raise UnauthorizedError(
            "Missing X-Actor-Email header.",
            details={"expected": "the email address of the person this call is for"},
        )
    try:
        validated = _ActorHeader(email=raw_email.strip().lower())  # type: ignore[arg-type]
    except ValidationError as exc:
        raise UnauthorizedError("X-Actor-Email is not a valid email address.") from exc
    return Actor(email=validated.email)


async def require_actor(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_actor_email: Annotated[str | None, Header(alias="X-Actor-Email")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> Actor:
    """Authenticate the channel, then identify the actor. FastAPI dependency.

    This is the only dependency a protected endpoint needs::

        async def read_issue(actor: Annotated[Actor, Depends(require_actor)]) -> ...:

    The order of the two checks is the security property: the token is verified
    first, and the actor header is parsed only afterwards.

    Raises:
        UnauthorizedError: if the token is absent or wrong, or the actor header
            is absent or malformed.
    """
    resolved = settings if settings is not None else get_settings()
    token = _extract_bearer_token(authorization)
    verify_service_token(token, resolved)
    return parse_actor(x_actor_email)

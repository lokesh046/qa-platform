"""Minting and verifying this server's own OAuth tokens.

Pure functions over strings: no Redis, no HTTP, no settings object. That is
deliberate, because this is the one piece of the OAuth work where a mistake is
a security hole rather than a bug, and a pure module can be tested exhaustively
with no infrastructure.

A token is ``<payload>.<signature>``, both base64url without padding. The
payload is compact JSON; the signature is HMAC-SHA256 over the **encoded**
payload bytes, keyed by ``MCP_TOKEN_SIGNING_KEY``.

Signing rather than storing (ADR-022) means verification needs no lookup, so a
tool call does not depend on Redis being up. The cost is that a token cannot be
withdrawn before it expires, which is why lifetimes are short and why
authorisation is re-checked by the backend on every single call. A token here
proves **who**, never **what they may do**.

What is deliberately *not* in a payload: anything secret, anything large, and
anything the backend would otherwise look up. It carries an email, the client
that was issued it, its scopes, its intended resource and an expiry.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Literal

_logger = logging.getLogger("mcp.tokens")

TokenKind = Literal["access", "refresh"]

_SEPARATOR: Final = "."
MAX_TOKEN_CHARS: Final = 4096
"""Refuse anything longer before parsing it.

An attacker controls the token string. Without a bound, a multi-megabyte value
is base64-decoded and JSON-parsed on every request to the token endpoint.
"""


class InvalidTokenError(Exception):
    """The token is absent, malformed, unsigned by us, or expired.

    One exception for every failure, carrying no detail about which. Telling a
    caller "bad signature" rather than "expired" tells them their forgery was
    well-formed, and the distinction is of no use to a legitimate client.
    """


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """What a verified token asserts.

    Attributes:
        kind: ``access`` or ``refresh``. Checked by the caller, because a
            refresh token presented as an access token must not work.
        email: The verified email GitHub returned, lowercased. The identity.
        client_id: The OAuth client the token was issued to.
        scopes: Granted scopes.
        resource: The resource the token is bound to, or ``None``.
        expires_at: Unix seconds.
        jti: A unique id, so two tokens issued in the same second differ and so
            a revocation can name one.
    """

    kind: TokenKind
    email: str
    client_id: str
    scopes: tuple[str, ...]
    resource: str | None
    expires_at: int
    jti: str


def _b64url_encode(raw: bytes) -> str:
    """Base64url without padding, as every OAuth-adjacent format uses."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    """Reverse ``_b64url_encode``, restoring the padding it stripped."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def mint_token(
    *,
    kind: TokenKind,
    email: str,
    client_id: str,
    scopes: tuple[str, ...] | list[str],
    resource: str | None,
    ttl_seconds: int,
    signing_key: str,
    now: float | None = None,
) -> tuple[str, TokenClaims]:
    """Create a signed token and return it with the claims it carries.

    Returning both means a caller never has to parse what it just created in
    order to record an expiry.

    Args:
        kind: ``access`` or ``refresh``.
        email: The verified identity. Lowercased here, so one person is one
            subject however GitHub cased it (the same rule as ADR-017).
        client_id: The registered client being issued the token.
        scopes: Granted scopes.
        resource: RFC 8707 resource indicator, or ``None``.
        ttl_seconds: Lifetime from now.
        signing_key: ``MCP_TOKEN_SIGNING_KEY``.
        now: Override for tests.

    Returns:
        ``(token, claims)``.

    Raises:
        ValueError: On an empty email, client id or signing key, or a
            non-positive ttl. These are programming errors, not caller input.
    """
    if not email.strip():
        raise ValueError("email is required")
    if not client_id.strip():
        raise ValueError("client_id is required")
    if not signing_key:
        raise ValueError("signing_key is required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    issued_at = int(now if now is not None else time.time())
    claims = TokenClaims(
        kind=kind,
        email=email.strip().lower(),
        client_id=client_id,
        scopes=tuple(scopes),
        resource=resource,
        expires_at=issued_at + ttl_seconds,
        jti=secrets.token_urlsafe(12),
    )
    payload: dict[str, Any] = {
        "t": claims.kind,
        "sub": claims.email,
        "cid": claims.client_id,
        "scp": list(claims.scopes),
        "res": claims.resource,
        "exp": claims.expires_at,
        "jti": claims.jti,
    }
    # separators= keeps it compact; sort_keys makes the encoding reproducible,
    # which matters only because it makes a test able to assert on it.
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{encoded}{_SEPARATOR}{_sign(encoded, signing_key)}", claims


def _sign(encoded_payload: str, signing_key: str) -> str:
    """HMAC-SHA256 the encoded payload, base64url encoded."""
    digest = hmac.new(signing_key.encode("utf-8"), encoded_payload.encode("ascii"), sha256).digest()
    return _b64url_encode(digest)


def verify_token(
    token: str | None,
    *,
    signing_key: str,
    expected_kind: TokenKind,
    now: float | None = None,
) -> TokenClaims:
    """Verify a token's signature, kind and expiry, and return its claims.

    The order matters: the signature is checked **before** the payload is
    trusted for anything, so an unsigned payload never reaches a comparison
    that could act on it.

    Args:
        token: The presented token. ``None`` and empty are ordinary failures,
            not programming errors, because this is caller input.
        signing_key: ``MCP_TOKEN_SIGNING_KEY``.
        expected_kind: Refuse a refresh token presented as an access token, and
            the reverse. Without this check the two are interchangeable, and a
            refresh token lives far longer.
        now: Override for tests.

    Raises:
        InvalidTokenError: Always, for every kind of failure.
    """
    if not token:
        raise InvalidTokenError("no token presented")
    if len(token) > MAX_TOKEN_CHARS:
        raise InvalidTokenError("token too long")

    encoded, separator, signature = token.partition(_SEPARATOR)
    if not separator or not encoded or not signature:
        raise InvalidTokenError("malformed token")

    # compare_digest, not ==, so the comparison does not return early on the
    # first wrong byte. Same reasoning as the service-token check in auth.py.
    if not hmac.compare_digest(signature, _sign(encoded, signing_key)):
        _logger.warning("token signature rejected")
        raise InvalidTokenError("signature rejected")

    try:
        payload = json.loads(_b64url_decode(encoded))
    except (ValueError, TypeError) as exc:
        # Reachable only with our own signature over a bad payload, so this
        # means a bug or a leaked key, not an ordinary forgery. Worth a log.
        _logger.error("signed token carried an unparseable payload")
        raise InvalidTokenError("malformed token") from exc

    if not isinstance(payload, dict):
        raise InvalidTokenError("malformed token")

    kind = payload.get("t")
    if kind != expected_kind:
        raise InvalidTokenError("wrong token kind")

    email = payload.get("sub")
    client_id = payload.get("cid")
    expires_at = payload.get("exp")
    if not isinstance(email, str) or not isinstance(client_id, str):
        raise InvalidTokenError("malformed token")
    if not isinstance(expires_at, int):
        raise InvalidTokenError("malformed token")

    moment = now if now is not None else time.time()
    if expires_at <= moment:
        raise InvalidTokenError("expired")

    raw_scopes = payload.get("scp")
    scopes: tuple[str, ...] = ()
    if isinstance(raw_scopes, list):
        scopes = tuple(s for s in raw_scopes if isinstance(s, str))
    resource = payload.get("res")
    jti = payload.get("jti")
    return TokenClaims(
        kind=expected_kind,
        email=email,
        client_id=client_id,
        scopes=scopes,
        resource=resource if isinstance(resource, str) else None,
        expires_at=expires_at,
        jti=jti if isinstance(jti, str) else "",
    )

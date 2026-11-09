"""Tests for ``mcp_server.tokens``.

This module is the one place in the OAuth work where a mistake is a security
hole rather than a bug: it decides whether a presented string is a identity
this server trusts. It is also pure, so there is no excuse for not covering
every refusal path.

The tests are written against the *contract* -- a token round trips, and
everything else is refused -- not against the encoding, so changing the payload
layout should not break them.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from mcp_server.tokens import (
    MAX_TOKEN_CHARS,
    InvalidTokenError,
    TokenClaims,
    mint_token,
    verify_token,
)

KEY = "signing-key-for-tests-0123456789abcdefgh"
OTHER_KEY = "a-different-signing-key-0123456789abcdef"


def issue(**overrides: object) -> tuple[str, TokenClaims]:
    """Mint an access token, overriding any argument."""
    kwargs: dict[str, object] = {
        "kind": "access",
        "email": "qa.one@acme-qa.co",
        "client_id": "client-abc",
        "scopes": ("qa:read",),
        "resource": "https://qa.example.org/mcp",
        "ttl_seconds": 3600,
        "signing_key": KEY,
    }
    kwargs.update(overrides)
    return mint_token(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_minted_token_verifies():
    token, minted = issue()
    verified = verify_token(token, signing_key=KEY, expected_kind="access")
    assert verified == minted


def test_every_claim_survives_the_round_trip():
    token, _ = issue(scopes=("qa:read", "qa:write"))
    c = verify_token(token, signing_key=KEY, expected_kind="access")
    assert c.email == "qa.one@acme-qa.co"
    assert c.client_id == "client-abc"
    assert c.scopes == ("qa:read", "qa:write")
    assert c.resource == "https://qa.example.org/mcp"
    assert c.kind == "access"


def test_the_email_is_lowercased():
    # The same rule as ADR-017: one person is one subject, however the upstream
    # cased it. Otherwise the audit trail splits again, one row per casing.
    token, minted = issue(email="QA.One@Acme-QA.co")
    assert minted.email == "qa.one@acme-qa.co"
    assert verify_token(token, signing_key=KEY, expected_kind="access").email == (
        "qa.one@acme-qa.co"
    )


def test_two_tokens_issued_together_are_different():
    # They differ by jti. Without it, two tokens minted in the same second for
    # the same user would be the identical string.
    first, _ = issue()
    second, _ = issue()
    assert first != second


def test_a_refresh_token_verifies_as_a_refresh_token():
    token, _ = issue(kind="refresh", ttl_seconds=86400)
    assert verify_token(token, signing_key=KEY, expected_kind="refresh").kind == "refresh"


def test_no_resource_is_allowed():
    token, _ = issue(resource=None)
    assert verify_token(token, signing_key=KEY, expected_kind="access").resource is None


def test_empty_scopes_are_allowed():
    token, _ = issue(scopes=())
    assert verify_token(token, signing_key=KEY, expected_kind="access").scopes == ()


# ---------------------------------------------------------------------------
# Refusals. Each one is a way somebody gets in if it does not hold.
# ---------------------------------------------------------------------------


def test_a_token_signed_with_another_key_is_refused():
    token, _ = issue(signing_key=OTHER_KEY)
    with pytest.raises(InvalidTokenError):
        verify_token(token, signing_key=KEY, expected_kind="access")


def test_a_tampered_payload_is_refused():
    # Re-encode a payload claiming to be somebody else, keeping the signature.
    token, _ = issue()
    encoded, _, signature = token.partition(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["sub"] = "attacker@elsewhere.example"
    forged = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + "."
        + signature
    )
    with pytest.raises(InvalidTokenError):
        verify_token(forged, signing_key=KEY, expected_kind="access")


def test_a_refresh_token_is_refused_where_an_access_token_is_expected():
    # The load-bearing one. A refresh token lives a day; if the two were
    # interchangeable, presenting one as an access token would extend the
    # shorter lifetime to the longer one.
    token, _ = issue(kind="refresh")
    with pytest.raises(InvalidTokenError):
        verify_token(token, signing_key=KEY, expected_kind="access")


def test_an_access_token_is_refused_where_a_refresh_token_is_expected():
    token, _ = issue(kind="access")
    with pytest.raises(InvalidTokenError):
        verify_token(token, signing_key=KEY, expected_kind="refresh")


def test_an_expired_token_is_refused():
    token, _ = issue(ttl_seconds=60, now=time.time() - 3600)
    with pytest.raises(InvalidTokenError):
        verify_token(token, signing_key=KEY, expected_kind="access")


def test_a_token_expiring_exactly_now_is_refused():
    # The boundary is deliberately exclusive: expires_at <= now fails.
    moment = 1_000_000_000.0
    token, minted = issue(ttl_seconds=60, now=moment)
    with pytest.raises(InvalidTokenError):
        verify_token(token, signing_key=KEY, expected_kind="access", now=minted.expires_at)
    # One second earlier it is still good.
    assert verify_token(token, signing_key=KEY, expected_kind="access", now=minted.expires_at - 1)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (None, "nothing presented"),
        ("", "empty string"),
        ("no-separator-at-all", "no dot"),
        (".", "both halves empty"),
        ("payload.", "no signature"),
        (".signature", "no payload"),
        ("not-base64!.sig", "payload is not base64"),
        ("a" * (MAX_TOKEN_CHARS + 1), "over the length bound"),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 30 else "long",
)
def test_malformed_input_is_refused(value: str | None, why: str):
    with pytest.raises(InvalidTokenError):
        verify_token(value, signing_key=KEY, expected_kind="access")
    assert why


def test_an_oversized_token_is_refused_before_it_is_decoded():
    # The bound exists so that an attacker-supplied megabyte is not base64
    # decoded and JSON parsed on every request.
    with pytest.raises(InvalidTokenError, match="too long"):
        verify_token("x" * 1_000_000, signing_key=KEY, expected_kind="access")


def test_a_payload_that_is_not_an_object_is_refused():
    encoded = base64.urlsafe_b64encode(b'["a list, not an object"]').decode().rstrip("=")
    from mcp_server.tokens import _sign

    with pytest.raises(InvalidTokenError):
        verify_token(f"{encoded}.{_sign(encoded, KEY)}", signing_key=KEY, expected_kind="access")


@pytest.mark.parametrize(
    "payload",
    [
        {"t": "access", "cid": "c", "exp": 9_999_999_999},
        {"t": "access", "sub": "a@b.co", "exp": 9_999_999_999},
        {"t": "access", "sub": "a@b.co", "cid": "c"},
        {"t": "access", "sub": 42, "cid": "c", "exp": 9_999_999_999},
        {"t": "access", "sub": "a@b.co", "cid": "c", "exp": "soon"},
    ],
    ids=["no-sub", "no-cid", "no-exp", "sub-not-a-string", "exp-not-an-int"],
)
def test_a_correctly_signed_but_incomplete_payload_is_refused(payload: dict[str, object]):
    # Reachable only with our own key, so it means a bug rather than a forgery.
    # It must still refuse rather than build claims out of None.
    from mcp_server.tokens import _sign

    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(InvalidTokenError):
        verify_token(f"{encoded}.{_sign(encoded, KEY)}", signing_key=KEY, expected_kind="access")


def test_junk_in_the_scopes_list_is_dropped_not_fatal():
    from mcp_server.tokens import _sign

    payload = {
        "t": "access",
        "sub": "a@b.co",
        "cid": "c",
        "exp": 9_999_999_999,
        "scp": ["qa:read", 7, None, "qa:write"],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    claims = verify_token(
        f"{encoded}.{_sign(encoded, KEY)}", signing_key=KEY, expected_kind="access"
    )
    assert claims.scopes == ("qa:read", "qa:write")


# ---------------------------------------------------------------------------
# Minting refuses nonsense from its own caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": ""},
        {"email": "   "},
        {"client_id": ""},
        {"signing_key": ""},
        {"ttl_seconds": 0},
        {"ttl_seconds": -1},
    ],
    ids=["no-email", "blank-email", "no-client", "no-key", "zero-ttl", "negative-ttl"],
)
def test_minting_rejects_a_programming_error(overrides: dict[str, object]):
    with pytest.raises(ValueError):
        issue(**overrides)


def test_no_part_of_the_signing_key_appears_in_a_token():
    token, _ = issue()
    assert KEY not in token
    assert KEY[:8] not in token

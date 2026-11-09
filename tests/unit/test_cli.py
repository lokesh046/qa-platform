"""Tests for ``app.cli``: argument parsing and normalisation only.

The commands themselves write to the database, so they are not covered here —
see ``tests/integration/README.md``. What *is* covered is everything that
happens before a connection is opened, which is where an operator's mistake
has to be caught: the default role, the normalisation of what they typed, and
the refusal of a malformed project key.

The default role matters most. ``add-user --role`` defaulting to anything other
than ``viewer`` would mean a typo grants more access than intended, and nothing
else in the system would notice.
"""

from __future__ import annotations

import argparse

import pytest

from app.cli import _build_parser, _normalise_email, _validate_project_key
from app.models.user import UserRole


def parse(*argv: str) -> argparse.Namespace:
    """Parse a command line, as ``main`` does."""
    return _build_parser().parse_args(list(argv))


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("QA.One@Acme-QA.co", "qa.one@acme-qa.co"),
        ("  qa.one@acme-qa.co  ", "qa.one@acme-qa.co"),
        ("QA.ONE@ACME-QA.CO", "qa.one@acme-qa.co"),
    ],
)
def test_an_email_is_lowercased_and_stripped(raw: str, expected: str):
    # users.email is stored lowercase with a CHECK enforcing it, so a
    # mixed-case address would otherwise hit a constraint violation and a
    # confusing error. Agrees with parse_actor and mcp_server.auth; ADR-017.
    assert _normalise_email(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("abc", "ABC"), ("  abc  ", "ABC"), ("AbC", "ABC"), ("PROJECT2024", "PROJECT2024")],
)
def test_a_project_key_is_uppercased_and_stripped(raw: str, expected: str):
    assert _validate_project_key(raw) == expected


@pytest.mark.parametrize("raw", ["", "A", "1AB", "AB-C", "AB C", "AB_C", "ABC-123", "ÅBC"])
def test_a_malformed_project_key_exits_with_an_explanation(raw: str):
    # Checked here as well as by the database constraint, so the operator gets
    # "ABC is the shape I need" rather than a Postgres CHECK violation.
    with pytest.raises(SystemExit) as excinfo:
        _validate_project_key(raw)
    assert "not a Jira project key" in str(excinfo.value)
    assert "for example ABC" in str(excinfo.value)


def test_an_issue_key_is_not_a_project_key():
    # The likely paste: somebody copies ABC-123 out of a ticket.
    with pytest.raises(SystemExit):
        _validate_project_key("ABC-123")


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_add_user_defaults_to_the_least_privileged_role():
    # A typo must not grant more access than intended.
    args = parse("add-user", "--email", "a@acme-qa.co", "--name", "A")
    assert args.role == UserRole.VIEWER.value


def test_add_user_accepts_every_defined_role():
    for role in UserRole:
        args = parse("add-user", "--email", "a@acme-qa.co", "--name", "A", "--role", role.value)
        assert args.role == role.value


def test_add_user_rejects_an_invented_role():
    # The enum is the allowlist. "superuser" is the sort of thing that gets
    # typed hopefully.
    with pytest.raises(SystemExit):
        parse("add-user", "--email", "a@acme-qa.co", "--name", "A", "--role", "superuser")


@pytest.mark.parametrize(
    "argv",
    [
        ("add-user", "--name", "A"),
        ("add-user", "--email", "a@acme-qa.co"),
        ("grant-access", "--email", "a@acme-qa.co"),
        ("grant-access", "--project", "ABC"),
        ("revoke-access", "--email", "a@acme-qa.co"),
        ("deactivate-user",),
        ("activate-user",),
    ],
)
def test_a_missing_required_argument_is_refused(argv: tuple[str, ...]):
    with pytest.raises(SystemExit):
        parse(*argv)


def test_no_command_is_refused():
    # Running the CLI bare must print usage, not do something.
    with pytest.raises(SystemExit):
        parse()


def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        parse("delete-everything")


@pytest.mark.parametrize(
    "command",
    [
        "add-user",
        "grant-access",
        "revoke-access",
        "deactivate-user",
        "activate-user",
        "list-users",
    ],
)
def test_every_documented_command_is_available(command: str):
    # Guards against the README and docs drifting from the parser.
    assert command in _build_parser().format_help()


def test_there_is_no_command_that_deletes_a_user():
    # Deliberate: audit_logs.actor_user_id is ON DELETE SET NULL, so deleting a
    # user would silently detach their history. Deactivation is the supported
    # operation, and it keeps the trail attributable.
    help_text = _build_parser().format_help()
    assert "delete-user" not in help_text
    assert "deactivate-user" in help_text


def test_list_users_takes_no_arguments():
    assert parse("list-users").command == "list-users"

"""Tests for every input model and configuration validator.

Four things are checked here, and the first two are the ones that carry a
security argument:

- **The issue-key pattern**, in both places that enforce it. The backend and
  the MCP server validate independently on purpose (the backend cannot trust
  the gatekeeper), so both are tested against the same table of bad keys.
- **``extra="forbid"``** on the input models. An ignored unknown field is how
  a caller smuggles something past review.
- **The ``mcp.`` action namespace** on ``POST /api/v1/audit``, which is what
  stops a token holder forging a row that looks like the backend's own.
- **Configuration validators**, which exist so a misconfiguration fails at
  startup rather than per request.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, format_validation_error
from app.models.audit_log import AuditOutcome
from app.modules.audit.schemas import AuditEvent, AuditWriteRequest
from app.modules.jira.schemas import IssueKeyPath
from mcp_server.config import McpSettings
from mcp_server.schemas import GetJiraIssueArgs
from tests.conftest import make_mcp_settings, make_settings

#: Keys every validator must accept.
GOOD_KEYS = ["ABC-123", "QA-7", "A1-1", "PROJECT2024-999999", "AB-0"]

#: Keys every validator must reject, with why. These are the interesting half:
#: each one is a shape somebody or something will eventually send.
BAD_KEYS = [
    ("abc-123", "lowercase project key"),
    ("Abc-123", "mixed case project key"),
    ("ABC123", "no hyphen"),
    ("ABC-", "no number"),
    ("-123", "no project key"),
    ("A-1", "single-character project key"),
    ("ABC-1x", "trailing characters"),
    ("ABC-12.3", "not an integer"),
    ("ABC-1 ", "trailing space"),
    (" ABC-1", "leading space"),
    ("ABC--1", "double hyphen"),
    ("ABC-1-2", "two hyphens"),
    ("1BC-1", "project key starting with a digit"),
    ("AB_C-1", "underscore in the project key"),
    ("ABC-1;DROP TABLE users", "sql fragment"),
    ("https://acme-qa.atlassian.net/browse/ABC-1", "a URL, not a key"),
    ("ABC-1\n", "trailing newline"),
    ("ABC-1/../DEF-2", "path traversal attempt"),
    ("", "empty"),
    ("AB", "too short for the minimum length"),
    ("A" * 70 + "-123", "longer than the maximum length"),
]


# ---------------------------------------------------------------------------
# The issue key, in both validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", GOOD_KEYS)
def test_backend_accepts_well_formed_keys(key: str):
    assert IssueKeyPath(issue_key=key).issue_key == key


@pytest.mark.parametrize("key", GOOD_KEYS)
def test_mcp_accepts_well_formed_keys(key: str):
    assert GetJiraIssueArgs(issue_key=key).issue_key == key


@pytest.mark.parametrize(("key", "why"), BAD_KEYS, ids=[why for _, why in BAD_KEYS])
def test_backend_rejects_malformed_keys(key: str, why: str):
    with pytest.raises(ValidationError):
        IssueKeyPath(issue_key=key)


@pytest.mark.parametrize(("key", "why"), BAD_KEYS, ids=[why for _, why in BAD_KEYS])
def test_mcp_rejects_malformed_keys(key: str, why: str):
    # The duplication is the point: the backend validates again because it
    # cannot trust that the caller was this gatekeeper.
    with pytest.raises(ValidationError):
        GetJiraIssueArgs(issue_key=key)


def test_the_two_validators_agree_on_every_case():
    # A regression guard on the shared constant. If one side's pattern were
    # edited in isolation, this fails even if both tables above still pass.
    for key in GOOD_KEYS + [key for key, _ in BAD_KEYS]:

        def accepted(model: type, value: str = key) -> bool:
            try:
                model(issue_key=value)
            except ValidationError:
                return False
            return True

        assert accepted(IssueKeyPath) == accepted(GetJiraIssueArgs), key


def test_unknown_argument_is_rejected_by_the_tool_model():
    # A language model that invents a plausible extra argument must be told,
    # not silently obeyed with the argument dropped.
    with pytest.raises(ValidationError) as excinfo:
        GetJiraIssueArgs(issue_key="ABC-1", project="ABC")
    assert "extra_forbidden" in str(excinfo.value)


def test_unknown_field_is_rejected_by_the_path_model():
    with pytest.raises(ValidationError):
        IssueKeyPath(issue_key="ABC-1", force_refresh=True)


def test_the_rejection_names_the_field_not_the_value():
    # What a validation error is allowed to contain: the field and the rule.
    # The submitted value does not go into an error body (docs/api.md).
    with pytest.raises(ValidationError) as excinfo:
        IssueKeyPath(issue_key="ABC-1;DROP TABLE users")
    first = excinfo.value.errors()[0]
    assert first["loc"] == ("issue_key",)
    assert first["type"] == "string_pattern_mismatch"


# ---------------------------------------------------------------------------
# Audit input models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["mcp.get_jira_issue", "mcp.a", "mcp.a_b", "mcp.a.b.c"])
def test_audit_accepts_namespaced_actions(action: str):
    body = AuditWriteRequest(action=action, outcome=AuditOutcome.ALLOWED)
    assert body.action == action


@pytest.mark.parametrize(
    "action",
    [
        "jira_issue.read",  # the backend's own action: forging this is the attack
        "mcp",
        "mcp.",
        "MCP.get_jira_issue",
        "mcp.Get_Jira_Issue",
        "mcp..a",
        "mcp.a-b",
        "mcp.a b",
        "api.jira_issue.read",
        "",
    ],
)
def test_audit_rejects_actions_outside_the_mcp_namespace(action: str):
    with pytest.raises(ValidationError):
        AuditWriteRequest(action=action, outcome=AuditOutcome.ALLOWED)


def test_audit_detail_accepts_scalars():
    body = AuditWriteRequest(
        action="mcp.get_jira_issue",
        outcome=AuditOutcome.ALLOWED,
        detail={"project_key": "ABC", "attempts": 2, "elapsed": 0.5, "cached": True, "note": None},
    )
    assert body.detail["attempts"] == 2


@pytest.mark.parametrize(
    "detail",
    [
        {"nested": {"a": 1}},
        {"list": ["a", "b"]},
        {"body": [{"password": "hunter2"}]},
    ],
)
def test_audit_detail_rejects_non_scalar_values(detail: dict[str, object]):
    # The reason for the restriction: `detail` is the most likely place a
    # request body — and therefore a secret — would eventually be written.
    with pytest.raises(ValidationError):
        AuditWriteRequest(action="mcp.get_jira_issue", outcome=AuditOutcome.ALLOWED, detail=detail)


@pytest.mark.parametrize("field", ["actor_email", "source", "occurred_at", "request_id"])
def test_audit_body_cannot_supply_server_set_fields(field: str):
    # All four are things a caller would otherwise be able to forge.
    with pytest.raises(ValidationError):
        AuditWriteRequest(
            action="mcp.get_jira_issue", outcome=AuditOutcome.ALLOWED, **{field: "anything"}
        )


def test_audit_rejects_an_unknown_outcome():
    with pytest.raises(ValidationError):
        AuditWriteRequest(action="mcp.get_jira_issue", outcome="maybe")


def test_audit_event_is_frozen():
    # An event is a record of something that already happened; mutating one
    # after construction would mean the thing audited is not the thing stored.
    event = AuditEvent(
        actor_email="qa.one@acme-qa.co",
        action="jira_issue.read",
        outcome=AuditOutcome.ALLOWED,
        source="api",
    )
    with pytest.raises(ValidationError):
        event.outcome = AuditOutcome.DENIED


def test_audit_event_rejects_an_unknown_field():
    with pytest.raises(ValidationError):
        AuditEvent(
            actor_email="qa.one@acme-qa.co",
            action="jira_issue.read",
            outcome=AuditOutcome.ALLOWED,
            source="api",
            ip_address="10.0.0.1",
        )


# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------


def test_settings_build_from_valid_values(settings: Settings):
    assert settings.app_env == "local"
    assert settings.jira_cache_ttl_seconds == 300


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://qa:pw@localhost/qa",  # would silently select psycopg2
        "postgres://qa:pw@localhost/qa",
        "postgresql+asyncpg://qa:pw@localhost/qa",
        "localhost:5432/qa",
    ],
)
def test_database_url_must_name_the_psycopg_driver(url: str):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(database_url=url)
    assert "postgresql+psycopg" in str(excinfo.value)


def test_a_raw_pydantic_error_does_echo_the_rejected_value():
    # Not a wish, a fact, and the reason format_validation_error exists:
    # str(ValidationError) appends input_value=... for every rejected field.
    # Any startup path that prints one directly prints the database password.
    with pytest.raises(ValidationError) as excinfo:
        make_settings(database_url="postgresql://qa:sup3rs3cret@localhost/qa")
    assert "sup3rs3cret" in str(excinfo.value)


@pytest.mark.parametrize(
    ("overrides", "secret"),
    [
        ({"database_url": "postgresql://qa:sup3rs3cret@localhost/qa"}, "sup3rs3cret"),
        ({"app_env": "staging", "internal_api_token": "tok3n-too-short"}, "tok3n-too-short"),
        ({"jira_base_url": "http://acme.atlassian.net"}, "acme.atlassian.net"),
    ],
)
def test_the_formatted_error_names_the_variable_but_not_the_value(
    overrides: dict[str, object], secret: str
):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(**overrides)
    rendered = format_validation_error(excinfo.value)
    assert secret not in rendered
    assert "input_value" not in rendered


def test_the_formatted_error_is_still_useful():
    with pytest.raises(ValidationError) as excinfo:
        make_settings(database_url="postgresql://qa:pw@localhost/qa")
    rendered = format_validation_error(excinfo.value)
    assert "DATABASE_URL" in rendered
    assert "postgresql+psycopg" in rendered


def test_the_formatted_error_reports_a_missing_variable_by_name():
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    rendered = format_validation_error(excinfo.value)
    for name in ("DATABASE_URL", "JIRA_BASE_URL", "INTERNAL_API_TOKEN"):
        assert name in rendered


def test_the_mcp_formatted_error_hides_the_token():
    with pytest.raises(ValidationError) as excinfo:
        make_mcp_settings(internal_api_token="tok3n-too-short")
    rendered = format_validation_error(excinfo.value)
    assert "tok3n-too-short" not in rendered
    assert "INTERNAL_API_TOKEN" in rendered


@pytest.mark.parametrize("url", ["http://acme.atlassian.net", "acme.atlassian.net", "https://"])
def test_jira_base_url_must_be_absolute_https(url: str):
    with pytest.raises(ValidationError):
        make_settings(jira_base_url=url)


def test_jira_base_url_loses_its_trailing_slash():
    # Paths are built by concatenation, so a trailing slash becomes `//rest`.
    assert make_settings(jira_base_url="https://acme.atlassian.net/").jira_base_url == (
        "https://acme.atlassian.net"
    )


@pytest.mark.parametrize("value", ["customfield_10040", "customfield_1"])
def test_acceptance_criteria_field_accepts_a_custom_field_id(value: str):
    assert make_settings(jira_acceptance_criteria_field=value).jira_acceptance_criteria_field == (
        value
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_acceptance_criteria_field_normalises_absence_to_none(value: str | None):
    assert make_settings(jira_acceptance_criteria_field=value).jira_acceptance_criteria_field is (
        None
    )


@pytest.mark.parametrize(
    "value",
    ["Acceptance Criteria", "customfield", "customfield_", "customfield_abc", "10040"],
)
def test_acceptance_criteria_field_rejects_a_display_name(value: str):
    # The likely mistake. It would never match, and the description fallback
    # would mask it, so it must fail loudly at startup.
    with pytest.raises(ValidationError) as excinfo:
        make_settings(jira_acceptance_criteria_field=value)
    assert "customfield_10040" in str(excinfo.value)


@pytest.mark.parametrize("prefix", ["api/v1", "/api/v1/", "api", ""])
def test_api_prefix_must_start_with_a_slash_and_not_end_with_one(prefix: str):
    with pytest.raises(ValidationError):
        make_settings(api_v1_prefix=prefix)


@pytest.mark.parametrize("url", ["http://localhost:6379", "localhost:6379", "memcached://x"])
def test_redis_url_must_use_a_redis_scheme(url: str):
    with pytest.raises(ValidationError):
        make_settings(redis_url=url)


def test_placeholder_credentials_are_allowed_locally():
    # .env.example must be usable as-is, which is the whole point of a template.
    settings = make_settings(app_env="local", jira_api_token="replace-with-your-token")
    assert "JIRA_API_TOKEN" in settings.placeholder_secret_fields()


@pytest.mark.parametrize("env", ["staging", "production"])
def test_placeholder_credentials_block_a_deployment(env: str):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(app_env=env, jira_api_token="replace-with-your-token")
    assert "placeholder" in str(excinfo.value)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_a_short_token_blocks_a_deployment(env: str):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(app_env=env, internal_api_token="short")
    assert "INTERNAL_API_TOKEN" in str(excinfo.value)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_statement_echoing_blocks_a_deployment(env: str):
    # Bound parameters can contain personal data.
    with pytest.raises(ValidationError) as excinfo:
        make_settings(app_env=env, db_echo=True)
    assert "DB_ECHO" in str(excinfo.value)


def test_database_url_safe_masks_the_password():
    settings = make_settings(database_url="postgresql+psycopg://qa:sup3rs3cret@db:5432/qa_platform")
    safe = settings.database_url_safe
    assert "sup3rs3cret" not in safe
    assert safe == "postgresql+psycopg://qa:***@db:5432/qa_platform"


def test_redis_url_safe_masks_a_password_and_leaves_a_plain_url_alone():
    assert make_settings(redis_url="redis://:hunter2@cache:6379/0").redis_url_safe == (
        "redis://:***@cache:6379/0"
    )
    assert make_settings(redis_url="redis://cache:6379/0").redis_url_safe == "redis://cache:6379/0"


def test_secrets_do_not_render_in_a_repr(settings: Settings):
    # SecretStr is what makes a stray log line or traceback safe.
    rendered = repr(settings)
    assert "jira-api-token-for-tests" not in rendered
    assert "**********" in rendered


# ---------------------------------------------------------------------------
# MCP configuration
# ---------------------------------------------------------------------------


def test_mcp_settings_build_from_valid_values(mcp_settings: McpSettings):
    assert mcp_settings.is_http is False
    assert mcp_settings.backend_base_url.endswith(".co")


def test_mcp_settings_do_not_require_database_or_jira_credentials():
    # The separation this class exists for: it builds with neither, so nothing
    # in mcp_server can read them by accident.
    settings = make_mcp_settings()
    assert not hasattr(settings, "database_url")
    assert not hasattr(settings, "jira_api_token")


@pytest.mark.parametrize("token", ["", "short", "x" * 31])
def test_mcp_rejects_an_obviously_unset_token(token: str):
    with pytest.raises(ValidationError) as excinfo:
        make_mcp_settings(internal_api_token=token)
    # The validator's own message reports the length, never the value. The
    # surrounding pydantic error does echo it, which is what
    # format_validation_error strips before anything is printed.
    message = str(excinfo.value)
    assert "INTERNAL_API_TOKEN" in message
    assert not token or token not in format_validation_error(excinfo.value)


@pytest.mark.parametrize("url", ["backend:8000", "ftp://backend", "http://", ""])
def test_mcp_backend_url_must_be_absolute_http(url: str):
    with pytest.raises(ValidationError):
        make_mcp_settings(backend_base_url=url)


def test_mcp_backend_url_loses_its_trailing_slash():
    assert make_mcp_settings(backend_base_url="http://api:8000/").backend_base_url == (
        "http://api:8000"
    )


@pytest.mark.parametrize("transport", ["https", "sse", "websocket", ""])
def test_mcp_transport_is_restricted_to_the_two_supported_ones(transport: str):
    with pytest.raises(ValidationError):
        make_mcp_settings(mcp_transport=transport)


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_mcp_port_must_be_a_real_port(port: int):
    with pytest.raises(ValidationError):
        make_mcp_settings(mcp_port=port)


def test_mcp_actor_email_must_be_an_address():
    with pytest.raises(ValidationError):
        make_mcp_settings(qa_actor_email="not-an-email")

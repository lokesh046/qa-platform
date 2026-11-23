"""Tests for the data model's shape.

These run with no database, so they cannot prove a CHECK constraint *works* --
only that it is still declared. That distinction matters: the constraints were
verified against Postgres by trying to violate each one (see the step log),
and these tests are what notices if somebody later deletes one.

The gap is the one ADR-018 records and `tests/integration/README.md` lists
first.
"""

from __future__ import annotations

import pytest

from app.core.database import Base
from app.models import (
    ApprovalDecision,
    ApprovalTarget,
    DocumentStatus,
    DocumentType,
    ItemStatus,
    JiraItemKind,
    ScriptStatus,
    UserRole,
)

TABLES = (
    "users",
    "user_project_access",
    "jira_connections",
    "jira_issues",
    "documents",
    "requirements",
    "scenarios",
    "open_questions",
    "jira_items",
    "test_cases",
    "scripts",
    "approvals",
    "jobs",
    "audit_logs",
)


def constraint_names(table: str) -> set[str]:
    """Every named CHECK and UNIQUE constraint on a table."""
    return {c.name for c in Base.metadata.tables[table].constraints if c.name} | {
        i.name for i in Base.metadata.tables[table].indexes if i.unique and i.name
    }


# ---------------------------------------------------------------------------
# Every table the brief names exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_the_table_is_registered(table: str):
    assert table in Base.metadata.tables


def test_no_table_was_added_without_being_listed_here():
    # So a new table has to be declared in this test too, which is where
    # somebody notices it needs constraints and a migration.
    assert set(Base.metadata.tables) - {"brd_documents"} == set(TABLES)


# ---------------------------------------------------------------------------
# The roles and states the brief names
# ---------------------------------------------------------------------------


def test_the_four_roles_exist():
    assert [r.value for r in UserRole] == ["viewer", "qa_engineer", "qa_lead", "admin"]


def test_a_document_is_a_brd_or_an_fds():
    assert {t.value for t in DocumentType} == {"brd", "fds"}


def test_document_states_cover_the_review_loop():
    assert {s.value for s in DocumentStatus} == {
        "draft",
        "in_review",
        "approved",
        "rejected",
        "superseded",
    }


def test_jira_items_are_stories_tasks_or_subtasks():
    assert {k.value for k in JiraItemKind} == {"story", "task", "subtask"}


def test_an_item_is_created_only_as_its_own_state():
    # "created" means it exists in Jira, which is different from "approved".
    # Collapsing them would make "did we write this?" unanswerable.
    assert {s.value for s in ItemStatus} == {"proposed", "approved", "rejected", "created"}


def test_changes_requested_is_distinct_from_rejected():
    # The first expects another version; the second does not.
    assert {d.value for d in ApprovalDecision} == {
        "approved",
        "rejected",
        "changes_requested",
    }


def test_every_approvable_thing_has_a_target_type():
    assert {t.value for t in ApprovalTarget} == {
        "document",
        "scenario",
        "jira_items",
        "test_cases",
        "script",
    }


def test_a_script_records_whether_it_linted():
    assert "lint_failed" in {s.value for s in ScriptStatus}


# ---------------------------------------------------------------------------
# The constraints that carry a rule. Named individually, because each one is
# a rule somebody could delete without any test noticing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "constraint", "rule"),
    [
        ("documents", "ck_documents_fds_has_source", "an FDS must name the BRD it came from"),
        ("documents", "ck_documents_git_commit_full_sha", "a commit is a full sha or nothing"),
        ("documents", "ck_documents_has_an_input", "a document has an issue or source text"),
        ("documents", "uq_documents_lineage_version", "one version number per artifact"),
        ("documents", "ck_documents_author_email_lowercase", "one person is one author"),
        ("jira_items", "ck_jira_items_only_stories_are_roots", "a subtask must have a parent"),
        ("jira_items", "ck_jira_items_created_has_jira_key", "created means it has a key"),
        ("jira_items", "uq_jira_items_idempotency_key", "a retry cannot create a duplicate"),
        ("test_cases", "uq_test_cases_idempotency_key", "a retry cannot create a duplicate"),
        ("scripts", "ck_scripts_lint_failed_has_output", "a failed lint must say why"),
        ("approvals", "ck_approvals_refusal_has_a_note", "a refusal must give a reason"),
        ("approvals", "ck_approvals_target_version_positive", "an approval names a version"),
        ("open_questions", "ck_open_questions_answer_is_complete", "no half-recorded answers"),
        ("jira_connections", "ck_jira_connections_token_not_empty", "a connection has a token"),
        ("jira_connections", "uq_jira_connections_user_cloud", "one connection per user per site"),
    ],
)
def test_the_constraint_is_still_declared(table: str, constraint: str, rule: str):
    assert constraint in constraint_names(table), f"lost the rule: {rule}"


# ---------------------------------------------------------------------------
# Server defaults, so the database is usable without the ORM
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("documents", "status"),
        ("documents", "version"),
        ("requirements", "position"),
        ("scenarios", "position"),
        ("open_questions", "position"),
        ("jira_items", "status"),
        ("jira_items", "position"),
        ("test_cases", "status"),
        ("test_cases", "position"),
        ("scripts", "status"),
        ("scripts", "language"),
        ("scripts", "framework"),
        ("jira_connections", "key_version"),
    ],
)
def test_the_column_has_a_database_default(table: str, column: str):
    # A Python-side default only works through the ORM. A migration, a repair
    # script or a psql session hits NOT NULL instead -- which is how this was
    # found, by a constraint test that could not insert a row.
    col = Base.metadata.tables[table].columns[column]
    assert col.server_default is not None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_the_refresh_token_column_holds_bytes_not_text():
    # Bytes, so no encoding step can lose one and no careless str() produces
    # something that looks readable.
    col = Base.metadata.tables["jira_connections"].columns["refresh_token_encrypted"]
    assert col.type.__class__.__name__ == "LargeBinary"


def test_no_table_stores_an_access_token_or_a_secret():
    # The access token lives in memory for one request; only the refresh
    # token is stored, and only encrypted.
    forbidden = {"access_token", "client_secret", "password", "api_token"}
    for table in Base.metadata.tables.values():
        assert not (forbidden & set(table.columns.keys())), table.name

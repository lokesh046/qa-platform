"""SQLAlchemy models.

Importing this package imports every model, which is what populates
``Base.metadata``. Alembic's ``env.py`` relies on that: a model in a module
nobody imports is invisible to ``--autogenerate``, and the symptom is a
migration that silently drops the table.

Add every new model module to the imports below.
"""

from app.models.approval import Approval, ApprovalDecision, ApprovalTarget
from app.models.audit_log import AuditLog, AuditOutcome, AuditSource
from app.models.base import CreatedAtMixin, TimestampMixin, TimestampTZ, UUIDPrimaryKeyMixin
from app.models.brd_document import TERMINAL_BRD_STATUSES, BrdDocument, BrdStatus
from app.models.delivery import (
    ItemStatus,
    JiraItem,
    JiraItemKind,
    Script,
    ScriptStatus,
    TestCase,
)
from app.models.document import (
    Document,
    DocumentStatus,
    DocumentType,
    OpenQuestion,
    Requirement,
    Scenario,
)
from app.models.jira_connection import JiraConnection
from app.models.jira_issue import ISSUE_KEY_PATTERN, JiraIssue
from app.models.job import TERMINAL_STATUSES, Job, JobStatus, JobType
from app.models.user import PROJECT_KEY_PATTERN, User, UserProjectAccess, UserRole

__all__ = [
    "ISSUE_KEY_PATTERN",
    "PROJECT_KEY_PATTERN",
    "TERMINAL_BRD_STATUSES",
    "TERMINAL_STATUSES",
    "Approval",
    "ApprovalDecision",
    "ApprovalTarget",
    "AuditLog",
    "AuditOutcome",
    "AuditSource",
    "BrdDocument",
    "BrdStatus",
    "CreatedAtMixin",
    "Document",
    "DocumentStatus",
    "DocumentType",
    "ItemStatus",
    "JiraConnection",
    "JiraIssue",
    "JiraItem",
    "JiraItemKind",
    "Job",
    "JobStatus",
    "JobType",
    "OpenQuestion",
    "Requirement",
    "Scenario",
    "Script",
    "ScriptStatus",
    "TestCase",
    "TimestampMixin",
    "TimestampTZ",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserProjectAccess",
    "UserRole",
]

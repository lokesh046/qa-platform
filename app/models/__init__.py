"""SQLAlchemy models.

Importing this package imports every model, which is what populates
``Base.metadata``. Alembic's ``env.py`` relies on that: a model in a module
nobody imports is invisible to ``--autogenerate``, and the symptom is a
migration that silently drops the table.

Add every new model module to the imports below.
"""

from app.models.audit_log import AuditLog, AuditOutcome, AuditSource
from app.models.base import CreatedAtMixin, TimestampMixin, TimestampTZ, UUIDPrimaryKeyMixin
from app.models.jira_issue import ISSUE_KEY_PATTERN, JiraIssue
from app.models.job import TERMINAL_STATUSES, Job, JobStatus, JobType
from app.models.user import PROJECT_KEY_PATTERN, User, UserProjectAccess, UserRole

__all__ = [
    "ISSUE_KEY_PATTERN",
    "PROJECT_KEY_PATTERN",
    "TERMINAL_STATUSES",
    "AuditLog",
    "AuditOutcome",
    "AuditSource",
    "CreatedAtMixin",
    "JiraIssue",
    "Job",
    "JobStatus",
    "JobType",
    "TimestampMixin",
    "TimestampTZ",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserProjectAccess",
    "UserRole",
]

"""AI QA Automation Platform backend.

Phase 1: configuration, logging, error handling, health endpoints and a
read-only Jira lookup. Later phases add BRD generation, test generation,
workflows and Playwright execution.
"""

#: Application version, reported by ``/api/v1/health``.
#:
#: Duplicated from ``pyproject.toml`` because this project is an application,
#: not an installed package (``[tool.uv] package = false``), so
#: ``importlib.metadata.version`` has nothing to read. Keep the two in sync;
#: a release check should enforce it.
__version__ = "0.1.0"

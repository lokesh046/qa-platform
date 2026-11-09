"""Liveness and readiness endpoints.

Follows the four-file module pattern minus ``repository.py``: health owns no
tables, so there are no queries to put behind a repository. Its "repository"
is the two probe helpers in ``app.core.database`` and ``app.core.redis``.
"""

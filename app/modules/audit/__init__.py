"""Append-only audit trail.

Records who asked for what, when, and whether they were allowed. Writes only;
nothing in this project updates or deletes an audit row, and Phase 1 has no
endpoint that reads them.

Has no ``router.py``: there is nothing to expose yet. Reading the trail is an
administrator's job, done with SQL, until something needs it over HTTP.
"""

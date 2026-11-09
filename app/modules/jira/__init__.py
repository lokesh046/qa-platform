"""Read-only access to Jira issues.

Follows the four-file module pattern, plus two modules the pattern does not
name:

- ``client.py`` — the HTTP gateway to Jira. A ``repository.py`` talks to our
  database; this talks to someone else's service, and the two have no failure
  modes in common.
- ``adf.py`` — Atlassian Document Format to plain text. Pure functions, no I/O,
  so the trickiest parsing in the module can be tested with a literal dict.
"""

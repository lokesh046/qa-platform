# Module: defects (placeholder)

**Status:** empty. Filled in **Phase 5**.

## Purpose
Jira write-back. Raises a defect in Jira for a failed test, attaches evidence,
and links the defect to the originating issue and test case. This is the first
module that writes to Jira, so it needs its own permission checks.

## Expected contents
`router.py`, `service.py`, `schemas.py`, `repository.py`.

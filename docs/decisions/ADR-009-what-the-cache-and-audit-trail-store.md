# ADR-009: What the Jira cache and the audit trail are allowed to store

Date: 2026-11-08
Status: Accepted

## Context

Two of the five tables in Step 5 hold data that originates outside this system
and will outlive the request that created it. Both invite the same shortcut —
"store the whole payload, decide what we need later" — and both make that
shortcut expensive to undo.

**`jira_issues`** is a read-through cache. A Jira Cloud issue fetched from
`/rest/api/3/issue/{key}` is a large JSON document. Beyond the summary and
description it carries the reporter and assignee (display names, email
addresses, avatar URLs, account ids), every comment with its author and
timestamps, attachment metadata and filenames, worklogs, change history, and
an open-ended set of custom fields whose contents nobody on this project
controls. A QA ticket routinely quotes production data in a reproduction step.

**`audit_logs`** has a `detail` JSONB column with no schema. It is the obvious
place to put "everything we know about this request", which is exactly how
tokens and request bodies end up in a table that is deliberately never
deleted.

The project rules are explicit: never log tokens, passwords or full request
bodies, and never include secrets in anything. Those rules are about logs,
which rotate. A database table does not rotate, so the same rules apply with
more force.

## Decision

**`jira_issues` stores extracted fields only. No raw payload column.**

The table holds `issue_key`, `project_key` (generated from the key),
`summary`, `description`, `acceptance_criteria`, `issue_type`, `status`,
`jira_updated_at` and `fetched_at`. Step 6 flattens Atlassian Document Format
to plain text before storing, so the text columns hold what a person would
read.

Not stored: reporter, assignee, comments, attachments, worklogs, change
history, custom fields, account ids, avatar URLs, and the raw JSON.

If a later phase needs another field, it gets its own column and its own
migration. Needing one field is not a reason to store all of them.

**`audit_logs.detail` holds only what a reader needs to understand the
decision.**

Allowed: the reason a request was denied (`{"reason": "no project access"}`),
which validation rule rejected an argument (`{"rule": "issue_key_format"}`),
whether the cache was hit, an upstream status code, a duration, a job id, a
count.

Forbidden: any token, password or credential; any `Authorization` header;
request or response bodies; the raw Jira payload or any part of it beyond an
issue key; free-text user input copied verbatim; and anything whose size is
not bounded by the code that writes it.

**The identifying columns are denormalised on purpose.** `actor_email` and
`action` are stored as text alongside the nullable `actor_user_id` foreign key,
which is `ON DELETE SET NULL`. Deleting a user nulls the link and leaves the
history readable. The same reasoning applies to `jobs.requested_by_email`.

**The table is append-only by convention.** Nothing in this project updates or
deletes an audit row. `occurred_at` is `server_default now()`, so an actor
cannot backdate their own record.

## Alternatives considered

**Store the raw Jira JSON in a `raw` JSONB column.** The standard cache design,
and genuinely useful: a later phase that needs the assignee gets it without a
re-fetch and without a migration, and a bug in the extraction code can be
fixed by re-parsing what is already stored rather than re-calling Jira.
Rejected on data handling. The payload's contents are not under this project's
control, it carries personal data for people who are not users of this
platform, and a cache table is the least likely place anyone will look when
answering a deletion request. The re-fetch this costs is one HTTP call against
a service the system already depends on.

**Store the raw payload but with known-sensitive keys stripped.** A denylist of
`comment`, `attachment`, `reporter`, and so on. Rejected: a denylist over a
schema nobody controls fails the first time Jira adds a field or a team adds a
custom field called "Customer Details". An allowlist of fields is the same
thing as having columns, which is the decision above.

**Keep the raw payload briefly, in Redis, with a TTL.** A reasonable middle
ground, and Redis is already in the stack. Not rejected on principle — but it
solves a problem nothing has yet, so it is not being built in Phase 1. If
re-parsing ever becomes the bottleneck, this is the thing to build.

**A hard foreign key from `audit_logs` to `users`, with `ON DELETE
RESTRICT`.** Guarantees every audit row is attributable. Rejected because it
makes deleting a user impossible while any audit row references them, which
in an append-only table means never — and that collides with the right to
erasure. `SET NULL` plus a denormalised email keeps the history while letting
the user record go.

**Give `detail` a Pydantic schema per action.** Enforceable, and it would make
the rules above machine-checked rather than documented. Rejected for Phase 1 as
premature: there is one action (`mcp.get_jira_issue`). Worth doing once there
are several, and a good candidate for the point at which the audit module gains
a `service.py`.

## Consequences

- Any Phase 2 or 3 feature needing a Jira field this table does not have
  requires a migration and a re-fetch. This is the main cost and it is
  deliberate.
- A deletion request is answerable: the personal data this system holds about a
  platform user is in `users`, and the audit trail keeps only their email.
  Nothing holds personal data about third parties who merely appear on a Jira
  ticket.
- `audit_logs.detail` has no schema, so the rules above are enforced by review,
  not by code. That is a real weakness and the most likely way this decision
  gets violated. It is in the Step 5 known issues.
- `audit_logs` grows without bound. No retention policy exists yet. Phase 1
  writes one row per MCP tool call, so this is not urgent, but a platform-wide
  retention decision is owed before this is deployed.
- `summary` and `description` still contain whatever the ticket author wrote,
  which may include production data. The cache inherits Jira's sensitivity for
  those fields; it does not add to it. Access is gated by
  `user_project_access`, checked before any read.

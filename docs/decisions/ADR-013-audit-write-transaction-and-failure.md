# ADR-013: How audit records are written, and what happens when that fails

Date: 2026-11-08
Status: Accepted

## Context

Step 6 left `audit_logs` unwritten: the brief assigns audit writing to Step 7,
at the MCP layer. That left a gap worth closing early — a direct HTTP call to
the backend, bypassing the MCP server, produced no audit record at all, and the
denials that ADR-009 calls "the valuable signal" existed only as application
log lines.

Writing them raised two questions that are easy to get wrong in opposite
directions.

**1. Which transaction does the audit write belong to?** The request already
has a session, injected by `get_session`. Reusing it is the obvious choice and
needs no new code.

It is also wrong, in a way that would have been invisible until someone went
looking for a denial that was never recorded. `get_session` rolls back on
exception. A denied read records its audit row and then raises `NotFoundError`
to report the refusal — so the rollback triggered by that exception would
discard the row. The outcomes that matter most, denials and upstream errors,
would be the only ones never persisted, while allowed reads would look fine.

**2. What should happen if the audit write itself fails?** Either the request
continues without a record, or it fails.

## Decision

**The audit write opens its own session and commits it independently.**
`app.modules.audit.service.record` takes no session parameter; it calls
`get_session_factory()` itself. The request's own transaction is untouched, so
an exception raised afterwards cannot undo the record.

**An audit failure propagates.** `record` logs at ERROR and re-raises. The
request then fails as an unhandled exception, becoming a generic 500 with no
data in the body.

**Every outcome is audited through the same call**, so this failure mode is
identical for an allowed read and a denied one. ADR-010's rule — that "no such
issue" and "no access" are indistinguishable — therefore still holds: if the
database is unable to accept an audit row, both return 500, not one 500 and one
404.

**`detail` accepts scalars only.** `AuditEvent.detail` is typed
`dict[str, str | int | float | bool | None]`, so a payload, a request body or a
nested object cannot be passed at all. `AuditEvent` sets `extra="forbid"`, so
a caller cannot add an unreviewed field by passing a keyword argument.

**Gate 1 is not audited.** A malformed issue key never reaches a user record,
the cache or Jira, so there is no access decision to record. Over HTTP, FastAPI
rejects it before the service is called.

**The repository exposes `insert_event` and nothing else.** No update, no
delete. The absence of those functions is the only thing enforcing
append-only in Phase 1.

## Alternatives considered

**Write the audit row in the request's session.** One connection, one
transaction, no new machinery, and the record is atomic with the work it
describes — which for an *allowed* read is a genuine advantage: you cannot end
up with a cached issue and no record of fetching it. Rejected because the
atomicity cuts the wrong way for refusals, which are reported by raising. A
hybrid — caller's session for success, separate session for failure — would get
both properties and was rejected as worse: two code paths for one concern, and
the next person adding an outcome has to know which kind they are adding.

**Swallow audit failures and log them.** Keeps the endpoint available when the
audit table is unwritable but the rest of the database is fine. Rejected: the
reason this platform routes through a gatekeeper at all is that someone wants
to know who read what. A system that serves data while silently failing to
record it is worse than one that stops, because the gap is invisible
afterwards. Noted as a trade in Consequences — this does make the endpoint
dependent on an audit write succeeding.

**Swallow failures for denials, propagate for allowed reads.** Tempting: a
denial has already refused the data, so losing its record costs less than
losing the record of a successful read. Rejected because it reintroduces
exactly the asymmetry ADR-010 removed. A database problem would turn allowed
reads into 500s while denials kept returning 404, which tells a caller
something about the request they made.

**Write audit rows asynchronously — a queue, a background task, or Redis.**
Removes the second connection from the request path and makes the write
failure-tolerant. Rejected for Phase 1: a buffered audit trail can lose records
on a crash, which is the one property it must not have, and it needs a worker
that does not exist until Phase 4. Worth revisiting if the extra connection
checkout becomes a bottleneck.

**A database trigger or `INSERT`-only role to enforce append-only.** Genuinely
better than a convention. Not done yet; recorded in the Step 6 known issues.

## Consequences

- **Denials are recorded.** Verified: three refusals in one run — unknown
  actor, deactivated account, missing grant — all persisted despite each
  raising. This is the assertion that would have failed under the obvious
  implementation.
- **Each audited request checks out two connections** from the pool instead of
  one, briefly and sequentially. With `DB_POOL_SIZE=5` and
  `DB_MAX_OVERFLOW=10` that is 15 per worker, so the per-request peak doubles
  while the ceiling does not change. It makes the existing note about
  `uvicorn --workers 4` against a default `max_connections` of 100 more
  pressing, not less.
- **An unwritable audit table takes the endpoint down.** That is the intended
  direction, and it is the main operational risk of this decision. It should be
  monitored: `app.audit` logging at ERROR means audit writes are failing.
- **One MCP tool call will produce two audit rows** once Step 7 lands: a
  `source=mcp` row for the tool invocation and a `source=api` row for the
  backend read. They are two distinct events at two trust boundaries, and
  `source` plus `action` tells them apart. Anyone counting reads must filter
  by `source`, or they will double-count.
- **`detail` being scalar-only is a real constraint.** Recording a structured
  reason means flattening it or adding a column. That is deliberate: ADR-009
  named this column as the most likely place a secret would be written, and
  the type now makes "paste the payload in" impossible rather than merely
  discouraged.

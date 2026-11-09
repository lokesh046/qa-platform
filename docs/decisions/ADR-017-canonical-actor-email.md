# ADR-017: One canonical form for an actor's email

Date: 2026-11-08
Status: Accepted

## Context

An actor reaches the backend as an `X-Actor-Email` header and is validated as
Pydantic's `EmailStr`. A Step 8 test asserted what ended up in the audit trail
when the header was `QA.One@Acme-QA.co`, and the answer was
`QA.One@acme-qa.co`.

That is correct behaviour on Pydantic's part. RFC 5321 makes the domain
case-insensitive and leaves the local part to the receiving mail server, so
`EmailStr` lowercases the domain and preserves the local part.

It is the wrong behaviour for this system, because of what the surrounding
code already does:

- `users.email` is stored lowercase and a CHECK constraint enforces it
  (Step 5). A mixed-case address **cannot** be registered.
- `repository.get_user_by_email` lowercases before comparing, so a mixed-case
  header still finds the right user and the request works normally.
- `audit_logs.actor_email` is denormalised and written from the header as
  given. So the trail recorded a form that no user row can hold.
- `mcp_server.auth` already lowercased, and so did `app/cli.py`. The backend's
  HTTP path was the only one that did not.

The consequence is in the audit trail, which is the part of this system that
exists to answer questions afterwards. `WHERE actor_email = 'qa.one@acme-qa.co'`
misses rows. Worse, the same person can be recorded under as many actors as
they have casings, so a run of denials — exactly what the trail is for — can be
spread across `qa.one@`, `QA.One@`, `Qa.oNe@` and look like unrelated events
from different people. Nothing stops a caller varying the casing deliberately,
and the service token is all it takes to choose the header (ADR-003).

## Decision

An actor email has one form everywhere: stripped and lowercased. It is applied
at both points where an `Actor` or an `AuditEvent` is constructed:

- `app.core.security.parse_actor` lowercases the header before validating it,
  which makes `Actor.email` canonical for every caller downstream — the
  repository lookup, the audit row and any future authorisation check.
- `AuditEvent` has a field validator on `actor_email` that does the same. This
  is deliberate duplication: it covers a row written from somewhere that is not
  an HTTP request, such as the CLI or a later background job, and it means the
  trail's invariant does not depend on every call site remembering.

Nothing is lost by canonicalising, because the database can only hold the
lowercase form anyway.

## Alternatives

- **Record the header exactly as presented, for fidelity.** This was the
  previous behaviour and has a real argument behind it: an audit trail should
  record what happened, not a tidied version. Rejected because the fidelity is
  worthless here — the casing is not a property of the actor, it is a property
  of whatever typed the header — and it is bought at the cost of the trail's
  main query being wrong.
- **Store both: the raw header and a canonical column.** Honest, and it would
  preserve the evidence that somebody varied the casing. Rejected for Phase 1
  as a column whose only use is to detect a thing nobody is yet looking for.
  Worth revisiting if the trail ever becomes an alerting source, and noted in
  ADR-009's list of what the trail does not store.
- **Lowercase at query time** (`WHERE lower(actor_email) = ...`). Rejected: it
  moves the requirement into every future query and silently stops using the
  index unless somebody also adds an expression index.
- **Treat the local part as case-sensitive and require an exact match against
  `users.email`.** Technically the most correct reading of the RFC. It would
  turn `QA.One@acme-qa.co` into a 403 for a registered user, which is a support
  ticket rather than a security improvement.

## Consequences

- One person is one actor in `audit_logs`, and the obvious query works.
- Rows written before this change may hold a mixed-case local part. There are
  none in any environment that matters yet, so no backfill migration is
  included. If one is ever needed it is a single `UPDATE audit_logs SET
  actor_email = lower(actor_email)` — which is a write to an append-only table,
  so it should be a deliberate, recorded operation rather than a migration
  nobody notices.
- A system that genuinely distinguishes `A@x` from `a@x` as different mailboxes
  cannot be represented. That is already true of `users.email` and its CHECK
  constraint; this makes the two consistent rather than introducing it.
- The three places that canonicalise — `parse_actor`, `AuditEvent` and
  `mcp_server.auth._parse_email` — must stay in agreement. The CLI's
  `_normalise_email` is a fourth. A test in each of
  `tests/unit/test_validation.py`, `tests/unit/test_jira_service.py`,
  `tests/unit/test_mcp_auth.py` and `tests/unit/test_cli.py` pins the
  behaviour, which is the only thing keeping four implementations honest.

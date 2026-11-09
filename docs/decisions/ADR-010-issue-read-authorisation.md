# ADR-010: Authorisation for issue reads, and what a refusal reveals

Date: 2026-11-08
Status: Accepted

## Context

`GET /api/v1/issues/{issue_key}` reads Jira through a single service account.
That account can see every project in the tenant — it has to, because it serves
every user. So the backend, not Jira, decides what each user may see. Two
questions follow.

**1. In what order are the checks done?** Calling Jira first and filtering the
result afterwards is the easier code to write, and it is what a caching layer
naturally suggests.

**2. What does a refusal look like?** When a user asks for `DEF-1` and holds no
grant for project `DEF`, the honest answer is 403 "you may not see this". The
cautious answer is 404 "there is no such issue".

Jira itself has an opinion on question 2: it returns **404 for an issue that
exists but is outside the caller's permissions**, deliberately, so that its own
API cannot be used to enumerate issues.

## Decision

**Four gates, in this order, each one passing before the next is attempted.**
Implemented in `app.modules.jira.service.get_issue`:

1. **Shape.** The issue key must match `^[A-Z][A-Z0-9]+-[0-9]+$`. Enforced by
   FastAPI's path validation and again by `IssueKeyPath`, because the service
   is also called by the MCP server in Step 7 and must not assume it was
   reached over HTTP.
2. **Identity.** The actor email must match an active row in `users`.
3. **Authorisation.** That user must hold a `user_project_access` grant for the
   project key derived from the issue key.
4. **Data.** Only now: serve the cached copy if it is within
   `JIRA_CACHE_TTL_SECONDS`, otherwise call Jira and cache the result.

Gate 3 strictly before gate 4 is the security property of the module.

**A missing grant returns 404 `not_found`, with a body byte-identical to a
genuinely missing issue** apart from the issue key in the message and the
request id.

**An unknown or deactivated actor returns 403 `forbidden`**, with a message
naming neither the issue nor the project.

## Alternatives considered

**Call Jira first, then check access.** Simpler control flow, and one fewer
database round trip on a cache miss. Rejected for three reasons, in increasing
order of seriousness. It wastes a Jira call and the rate-limit budget it
consumes. It makes response timing a side channel: "issue exists but you can't
see it" would take a Jira round trip, while "no such project" would return
immediately. And it puts the issue's contents in memory inside the request that
is about to refuse it, so one mistaken branch — an early return, a logging
statement, an error handler that echoes context — leaks the data rather than
merely the fact of its existence.

**403 for a missing grant.** Clearer for the user, much easier to support:
"you don't have access to DEF" is actionable, while "DEF-1 was not found" sends
someone hunting for a typo. Rejected because it turns the endpoint into an
oracle. A caller with a valid service token can walk `AAA-1`, `AAB-1`, … and
separate the projects that exist from the ones that do not, then walk issue
numbers inside a real project to learn how much work it holds. None of that
requires any grant. The support cost is real and is mitigated instead by the
audit trail: a denial is recorded with the actor, the project and the issue
key, so an administrator can answer "why can't I see this?" in one query.

**403 for a missing grant, 404 only for a non-existent issue.** The precise
version of the above. Rejected for the same reason — the distinction *is* the
leak, regardless of which code carries it.

**404 for an unknown actor too, for consistency.** Rejected. An unregistered
account is not a statement about any issue, so refusing it plainly leaks
nothing about the data, and "this account is not registered on the QA platform"
is the single most useful error this API can return during rollout. It does
confirm whether an email is registered, to a caller who already holds the
service token; that is accepted.

**Enforce access with Postgres row-level security.** Structural rather than
procedural, and immune to a forgotten check. Rejected for Phase 1: the
application connects as one database user, so RLS would need the actor pushed
into a session variable on every checkout, and `jira_issues` is a shared cache
with no owning user to key a policy on. Worth reconsidering if the number of
tables carrying per-user data grows.

## Consequences

- A user who genuinely mistypes a project key gets "not found" rather than
  "no access", and support must read the audit trail to tell the two apart.
  This is the cost of the decision and it is paid by administrators, not by
  attackers.
- No Jira call is made for a project the user cannot see. Verified: the Step 6
  harness asserts the mocked Jira received zero requests across every
  unauthenticated, unknown-actor, inactive-actor and ungranted-project case.
- The cache is populated only by authorised reads, so `jira_issues` never
  contains an issue that no user was entitled to fetch.
- Gate 2 means nothing works until a user exists. See ADR-012.
- The ordering is a convention enforced by reading one function. There is no
  mechanism that would catch someone reordering it, which is the main
  maintenance risk; the Step 6 known issues record it.

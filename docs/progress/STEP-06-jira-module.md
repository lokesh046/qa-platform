# Step 06: The jira module and the first real endpoint

Date: 2026-11-08
Phase: 1

## Goal

Implement `GET /api/v1/issues/{issue_key}`: authorise the caller, read the
issue from Jira Cloud with the service account, flatten its rich text to plain
text, cache it in `jira_issues`, and serve the cached copy while it is fresh.

## What was done

Created:

- `app/modules/jira/router.py` — the endpoint. Validates the key, resolves the
  actor, calls the service.
- `app/modules/jira/service.py` — the four authorisation gates and the
  cache-or-fetch decision. The order of operations here is the security
  property of the module.
- `app/modules/jira/repository.py` — the module's only SQLAlchemy. User
  lookup, grant check, cache read, and an `INSERT ... ON CONFLICT` upsert.
- `app/modules/jira/schemas.py` — `IssueRead`, `IssueKeyPath`.
- `app/modules/jira/client.py` — the HTTP gateway to Jira. Basic auth from the
  service account, bounded timeouts and pool, and a mapping from every Jira
  failure onto an `AppError`.
- `app/modules/jira/adf.py` — Atlassian Document Format to plain text, plus
  acceptance-criteria section extraction. Pure functions.
- `app/cli.py` — operator CLI for seeding users and grants.
- `docs/decisions/ADR-010-issue-read-authorisation.md` — gate order, and why a
  missing grant returns 404.
- `docs/decisions/ADR-011-acceptance-criteria-and-adf.md` — where acceptance
  criteria come from, and how untrusted ADF is handled.
- `docs/decisions/ADR-012-operator-cli-for-seeding.md` — why a CLI and not
  admin endpoints.

Changed:

- `app/core/config.py` — added `jira_acceptance_criteria_field`, validated to
  look like a Jira custom field id.
- `app/core/logging.py` — raised the `httpx` logger to WARNING so outbound
  URLs are not logged. See Security notes.
- `app/main.py` — included the jira router; shutdown now also closes the Jira
  HTTP pool.
- `app/modules/jira/router.py` and `app/core/errors.py` — replaced the
  deprecated `HTTP_422_UNPROCESSABLE_ENTITY` constant.
- `.env`, `.env.example` — added `JIRA_ACCEPTANCE_CRITERIA_FIELD`, and changed
  the placeholder `JIRA_BASE_URL` to a host that cannot resolve. See Security
  notes; this one matters.
- `docs/api.md` — the endpoint, with every field and every error.
- `README.md`, `CHANGELOG.md`, `app/modules/README.md`.

## How it works

### The four gates

`service.get_issue` is a sequence of checks, each of which must pass before the
next is attempted:

1. **Shape.** The issue key must match `^[A-Z][A-Z0-9]+-[0-9]+$`. FastAPI
   rejects a bad key before any handler code runs, and `IssueKeyPath` checks it
   again inside the service — because Step 7's MCP server calls the service
   directly and must not be trusted to have validated anything.
2. **Identity.** The actor email must match an active row in `users`.
3. **Authorisation.** That user must hold a `user_project_access` grant for the
   project key derived from the issue key.
4. **Data.** Only now: serve the cache if it is fresh, else call Jira.

**Gate 3 strictly before gate 4 is the point of the whole module.** The Jira
service account can see every project in the tenant, so checking afterwards
would waste a Jira call, make response timing a side channel, and put the
issue's contents in the memory of a request that is about to refuse it. The
harness asserts the mocked Jira received **zero** requests across every
unauthenticated, unknown-actor, inactive-actor and ungranted-project case.

A missing grant returns **404**, with a body identical to a genuinely missing
issue. ADR-010 has the reasoning and the alternatives; the short version is
that a 403 would let any holder of the service token enumerate which projects
and issues exist without holding a single grant.

### Why the module has six files, not four

The pattern is `router` / `service` / `schemas` / `repository`. Two more:

- **`client.py`** — a repository talks to our database; this talks to somebody
  else's service. They share no failure modes, so merging them would make
  `service.py` read as two kinds of query that happen to look alike rather than
  "ask the cache, else ask Jira".
- **`adf.py`** — pure functions over a dict. This is the trickiest parsing in
  the project and the component most likely to meet a shape nobody
  anticipated, so it is isolated where it can be tested with a literal.

### Acceptance criteria

Jira Cloud has no acceptance-criteria field. `JIRA_ACCEPTANCE_CRITERIA_FIELD`
optionally names the tenant's custom field (`customfield_10040` and similar);
when set and non-empty, it wins. Otherwise `adf.extract_acceptance_criteria`
finds a heading matching `acceptance criteria` (loosely — "Acceptance", "AC:"
also match) and returns everything until the next heading at the same level or
shallower. Null when neither exists.

Extraction works on the ADF node tree, not the flattened text: a heading is an
unambiguous node type, whereas in plain text "Acceptance Criteria" could be a
list item.

### ADF, and treating it as untrusted

Jira's REST API v3 returns rich text as a JSON tree of typed nodes. `adf.py`
walks it, rendering paragraphs and headings as blocks, lists as `- item` or
`1. item` lines, tables as pipe-separated rows, and mentions as their display
text. Unknown node types are **descended into rather than dropped**, because a
future ADF node will almost certainly still carry its text in `content`.

The content of that tree was written by whoever filed the ticket, which on an
internal QA board can include a customer. Two limits follow: `MAX_DEPTH = 24`
and `MAX_OUTPUT_CHARS = 60_000`, with truncation marked rather than silent. A
200-level-deep document returns `None` instead of exhausting the stack.

### Caching

`fetched_at` is compared against `JIRA_CACHE_TTL_SECONDS`. `0` disables the
cache, which is the natural reading and useful while debugging. The response
field `from_cache` says which path was taken, so this is observable rather than
guessed.

The write is a single `INSERT ... ON CONFLICT DO UPDATE`, not read-then-write.
Two concurrent requests for the same uncached issue is the normal case — a user
asks, and the MCP server asks on their behalf — and the read-then-write version
loses that race with a unique-constraint violation. `project_key` appears in
neither the insert nor the update, because it is a generated column and
Postgres rejects writing it. That is deliberate: it feeds the permission check.

### Only the needed fields are requested

The `fields` query parameter lists exactly `summary`, `description`,
`issuetype`, `status`, `updated`, plus the AC field when configured. Not asking
for comments, attachments, worklogs and change history is the cheapest possible
guarantee that ADR-009's "do not store them" is never violated by accident.

## How to run / verify

Commands below are bare, with expected output in prose. Interactive `zsh` does
not treat `#` as a comment unless `interactive_comments` is set.

Start the stack and apply the schema:

```
docker compose up -d api
uv run alembic upgrade head
```

Nothing can be authorised until a user exists. Seed one:

```
docker compose exec api python -m app.cli add-user --email qa.one@example.com --name "QA One" --role qa_engineer
docker compose exec api python -m app.cli grant-access --email qa.one@example.com --project ABC
docker compose exec api python -m app.cli list-users
```

Expect `created user ...`, `granted ... access to ABC`, then a table showing
the user with `ABC` under PROJECTS. Re-running either command is safe; it
prints `already exists` or `already granted`.

Export the token once:

```
set -a
. ./.env
set +a
```

No token:

```
curl -s http://127.0.0.1:8000/api/v1/issues/ABC-1
```

Expect HTTP 401 and `"code":"unauthorized"`, `"message":"Missing Authorization header."`

Valid token, unregistered actor:

```
curl -s -H "Authorization: Bearer $INTERNAL_API_TOKEN" -H "X-Actor-Email: stranger@example.com" http://127.0.0.1:8000/api/v1/issues/ABC-1
```

Expect HTTP 403 and `"This account is not registered on the QA platform."`

Registered actor, project they have no grant for:

```
curl -s -H "Authorization: Bearer $INTERNAL_API_TOKEN" -H "X-Actor-Email: qa.one@example.com" http://127.0.0.1:8000/api/v1/issues/DEF-1
```

Expect HTTP 404 and `"Issue DEF-1 was not found."` — the same shape a genuinely
missing issue returns. The container log carries the real reason:

```json
{"level": "WARNING", "logger": "app.jira.service", "message": "project access denied", "actor": "qa.one@acme-qa.co", "project_key": "DEF", "issue_key": "DEF-1"}
```

Malformed key:

```
curl -s -H "Authorization: Bearer $INTERNAL_API_TOKEN" -H "X-Actor-Email: qa.one@example.com" http://127.0.0.1:8000/api/v1/issues/abc-1
```

Expect HTTP 422, `"code":"validation_error"`, and a `details.fields` entry
naming the pattern. The submitted value is **not** echoed.

Granted project, with `JIRA_BASE_URL` still a placeholder:

```
curl -s -H "Authorization: Bearer $INTERNAL_API_TOKEN" -H "X-Actor-Email: qa.one@example.com" http://127.0.0.1:8000/api/v1/issues/ABC-1
```

Expect HTTP 502 and `"Could not reach Jira."` That is the correct result and it
proves all four gates passed and the Jira call was attempted. Against a real
tenant, set `JIRA_BASE_URL`, `JIRA_SERVICE_EMAIL` and `JIRA_API_TOKEN` and
expect 200.

Lint, format and tests:

```
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Expect `All checks passed!`, `73 files already formatted`, and `no tests ran`.

Behaviour was verified by a throwaway script with 89 assertions against a
mocked Jira (`respx`) and the live Postgres: happy path and caching, the three
acceptance-criteria sources, every authorisation gate including the zero-Jira-
call assertions, nine malformed keys, eleven Jira failure modes, the TTL in
both directions, secret hygiene across every request, and the absence of any
raw-payload column. See Known issues — it is not a committed test.

## Decisions and trade-offs

**A missing grant returns 404, not 403.** ADR-010. Costs support clarity, buys
non-enumerability. Mitigated by the denial being logged with actor, project and
issue key.

**An unknown actor returns 403, not 404.** An unregistered account is not a
statement about any issue, so refusing it plainly leaks nothing about the data,
and it is the most useful error this API can return during rollout. It does
confirm to a service-token holder whether an email is registered; accepted.

**Acceptance criteria: custom field, else description heading.** ADR-011. The
field-only option returns null for every issue until someone finds the tenant's
field id; the description-only option fails the teams who were most careful.

**A CLI, not admin endpoints, for seeding.** ADR-012. Admin endpoints need a
way to decide who is an administrator, which the brief defers — and
token-only protection would let any MCP caller grant themselves every project,
making ADR-010's gate chain decorative.

**The ADF parser is hand-written.** The available packages are thin,
unmaintained, or render HTML. The needed subset is about 200 lines, and the
alternative is depending on somebody's weekend project for the component that
parses untrusted input.

**Jira's 404 is passed through as our 404.** Jira deliberately returns 404
rather than 403 for an issue outside the caller's permissions, so "missing" and
"service account cannot see it" are indistinguishable from here. Treating it as
not-found is the only option and also the right answer.

**Jira 401 and 403 become our 502, not 401 or 403.** Those mean *this
service's* credentials are wrong or insufficient. That is an operator problem,
never the end user's, so the caller gets a generic "Jira rejected this
service's credentials" and the operator gets an ERROR log line naming the two
environment variables to check.

**One pooled `httpx.AsyncClient` for the process.** A client per request throws
away connection reuse and the TLS handshake with it, which against a remote API
is most of the latency. Closed in the lifespan.

**`follow_redirects=False`.** Jira answers an unauthenticated request with a
redirect to its login page. Following it would turn a 401 into a confusing 200
of HTML.

## Security notes

- **No Jira call happens before authorisation.** Verified across every refusal
  path: the mocked Jira recorded zero requests for unauthenticated calls,
  unknown actors, inactive accounts, ungranted projects and malformed keys.
- **Refusals do not leak existence.** The denied and genuinely-missing bodies
  were compared field by field and are identical apart from the issue key in
  the message and the `request_id`.
- **The service account's token never leaves the client.** It is attached by
  `httpx.BasicAuth` on the pooled client, so no call site handles it. Asserted
  absent from every log line and every response body across all 89 assertions,
  along with the internal API token, and no `Authorization:` or `Basic ` string
  appears in any log.
- **`httpx` was logging every outbound URL at INFO.** Observed in the container
  log during this step. No credential was in it — httpx does not log headers —
  but "never log tokens" is easier to hold as an invariant than as something to
  remember whenever a new upstream call is added. `configure_logging` now
  raises that logger to WARNING; our own client and service log what matters.
- **The placeholder `JIRA_BASE_URL` was reaching Atlassian.** `.env.example`
  shipped `https://your-tenant.atlassian.net`. `*.atlassian.net` has
  **wildcard DNS**, so that host resolves, and the container made a real HTTPS
  request to Atlassian — which answered 404, making the misconfiguration look
  like a missing issue. An unconfigured install silently talking to a third
  party is wrong regardless of what it sends. Both files now use
  `https://your-tenant.invalid`; RFC 2606 guarantees `.invalid` never resolves,
  so the same request now fails fast with 502 "Could not reach Jira."
- **Upstream error text never reaches the caller.** Jira's response body is
  never forwarded. Each failure maps to a fixed message; the status code and
  issue key go to the log.
- **Two bounds on untrusted input:** `MAX_RESPONSE_BYTES = 2_000_000` on the
  Jira response, and `MAX_DEPTH` / `MAX_OUTPUT_CHARS` in the ADF parser.
  Verified a 200-level-deep document returns `None` rather than raising.
- **Only five fields are requested from Jira,** so comments, attachments,
  worklogs and change history never cross the wire. The harness asserts
  `jira_issues` has no `raw`, `payload`, `comment`, `attachment`, `reporter` or
  `assignee` column.
- **`project_key` cannot be forged.** It is a generated column, excluded from
  both halves of the upsert, and it is what the permission check reads.
- **A 422 does not echo the submitted key,** so a hostile value cannot be
  reflected back. Checked with `ABC-1;DROP TABLE users` and
  `../../etc/passwd`, both rejected before reaching the database or Jira.
- **The service token is compared with `hmac.compare_digest`,** unchanged from
  Step 3. A valid prefix is rejected; asserted.
- **The CLI writes `granted_by_id = NULL`,** because it cannot know who ran it.
  A grant is therefore attributable to a shell session, not to a person. See
  Known issues.


## Addendum: audit records at the API layer

Added after the step was first completed, closing the first item in Known
issues below. The brief assigns audit writing to Step 7, at the MCP layer; that
would have left a direct HTTP call to the backend producing no audit record at
all, and the denials ADR-009 calls "the valuable signal" existing only as log
lines.

### What was added

- `app/modules/audit/schemas.py` — `AuditEvent`, with `extra="forbid"` and
  `detail` typed `dict[str, str | int | float | bool | None]`.
- `app/modules/audit/repository.py` — `insert_event`, and nothing else. No
  update, no delete.
- `app/modules/audit/service.py` — `record`, which opens its own session.
- `app/modules/jira/service.py` — an audit call on every exit path, and a new
  `source` parameter so Step 7 can pass `AuditSource.MCP`.
- `docs/decisions/ADR-013-audit-write-transaction-and-failure.md`.

The module has no `router.py`: nothing reads the trail over HTTP yet. That is
an administrator's job, done with SQL.

### The bug this avoids

The obvious implementation — write the audit row in the request's session — is
wrong, and wrong invisibly. `get_session` rolls back on exception. A denied
read records its row and then raises `NotFoundError` to report the refusal, so
that rollback would discard the row. **Allowed reads would look fine while
denials were silently never recorded**, which is precisely backwards from what
the table is for.

`record` therefore takes no session. It calls `get_session_factory()` itself
and commits independently, so the request's transaction cannot undo it. The
harness asserts three refusals in one run — unknown actor, deactivated account,
missing grant — all persist despite each raising.

### Failure is fail-closed

An audit write failure is logged at ERROR and re-raised, so the request becomes
a generic 500 and no data is returned. A gatekeeper that cannot record what it
did should not quietly carry on.

Because every outcome goes through the same call, this failure is identical for
an allowed read and a denied one, so ADR-010's indistinguishability rule still
holds: an unwritable audit table returns 500 for both, not 500 for one and 404
for the other. The trade is that the endpoint now depends on an audit write
succeeding; ADR-013 records why that direction was chosen.

### What is recorded

| Outcome | `outcome` | `detail` |
| --- | --- | --- |
| Served from cache | `allowed` | `cache: hit` |
| Fetched from Jira | `allowed` | `cache: miss`, `refreshed` |
| Actor not in `users` | `denied` | `reason: actor_not_registered` |
| Actor deactivated | `denied` | `reason: actor_inactive` |
| No project grant | `denied` | `reason: no_project_access`, `project_key` |
| Jira failed | `error` | `reason: <error code>` |

A malformed issue key is **not** audited: it never reaches a user record, the
cache or Jira, so there is no access decision to record.

Nothing is recorded for a request that fails authentication, because the actor
is not yet known.

### Verifying it

With the stack up and a user seeded:

```
curl -s -o /dev/null -H "Authorization: Bearer $INTERNAL_API_TOKEN" -H "X-Actor-Email: qa.one@example.com" http://127.0.0.1:8000/api/v1/issues/DEF-1
docker compose exec -T postgres psql -U qa -d qa_platform -c "select occurred_at::time(0), actor_email, action, outcome, source, resource_id, detail from audit_logs order by id;"
```

Expect a row like:

```
 occurred_at |    actor_email    |     action      | outcome | source | resource_id |                        detail
-------------+-------------------+-----------------+---------+--------+-------------+-------------------------------------------------------
 12:21:54    | qa.one@acme-qa.co | jira_issue.read | denied  | api    | DEF-1       | {"reason": "no_project_access", "project_key": "DEF"}
```

A request with no token adds no row.

Verified by a throwaway script with 49 assertions: every outcome above, the
three-refusals-survive-rollback case, `request_id` matching the HTTP response,
no rows for unauthenticated or malformed requests, and that no token, summary
or description reaches the `detail` column.

## Known issues / TODO

- ~~No audit rows are written yet.~~ **Closed by the addendum above.** Reads
  through the API are now audited. Step 7 adds a second, `source=mcp` row for
  the tool invocation itself, so one MCP call will produce two rows for two
  distinct events — anyone counting reads must filter on `source`.
- **Each audited request now checks out two database connections** instead of
  one, briefly and sequentially, because the audit write uses its own
  transaction. The pool ceiling is unchanged but the per-request peak doubles,
  which makes the `uvicorn --workers 4` note below more pressing.
- **An unwritable `audit_logs` table takes the endpoint down,** by design
  (ADR-013). Monitor for `app.audit` logging at ERROR.
- **Append-only is still only a convention.** `app/modules/audit/repository.py`
  exposes no update or delete, and that is the whole enforcement. A separate
  database role with `INSERT` and `SELECT` only would make it structural.
- **No audit retention policy.** Unchanged from Step 5, but now actually
  relevant: rows are accumulating.
- **Still no committed tests,** now across four steps. The highest-value
  targets, in order: `adf.py` (pure functions, no fixtures needed), the gate
  order in `service.py` (an authorisation property with no regression test),
  and `app/cli.py` (the only thing that can grant access, and untested).
- **The gate order is a convention.** Nothing would catch someone moving the
  Jira call above the access check. A test asserting zero upstream calls on a
  denied read is the fix, and it exists only in the throwaway harness.
- **The acceptance-criteria heading regex is loose** — "Acceptance", "AC:" all
  match. A false positive returns the wrong section rather than failing.
- **`granted_by_id` is always NULL.** The CLI has no operator identity. Fix
  when admin endpoints arrive.
- **No rate limiting on this endpoint.** A loop with a valid token and one
  grant can drive Jira calls up to the TTL boundary. `RateLimitedError` exists
  and is only ever raised by relaying Jira's own 429.
- **`JIRA_CACHE_TTL_SECONDS` is global,** so a fast-moving board and a stable
  one get the same freshness. Per-project TTL would need a schema change.
- **No negative caching.** A missing issue is re-requested from Jira on every
  call, so a typo in a loop reaches Jira every time.
- **Nothing invalidates the cache.** `JobType.JIRA_REFRESH` exists for a
  refresh job that does not yet exist; `force_refresh` is implemented in the
  service but exposed nowhere.
- **`mcp` service still cannot start** (needs Step 7), so
  `docker compose up -d api` remains the working command.
- **No mypy.** Carried over from Step 3.

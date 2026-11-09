# Step 05: SQLAlchemy models and the first Alembic migration

Date: 2026-11-08
Phase: 1

## Goal

Define the five tables Phase 1 needs — `users`, `user_project_access`,
`jira_issues`, `jobs`, `audit_logs` — as SQLAlchemy models, and ship them as a
reviewed Alembic migration that runs against the live Postgres.

## What was done

Created:

- `app/models/base.py` — `UUIDPrimaryKeyMixin`, `CreatedAtMixin`,
  `TimestampMixin`, the shared `TimestampTZ` type, and `enum_column()`.
- `app/models/user.py` — `User`, `UserProjectAccess`, `UserRole`,
  `PROJECT_KEY_PATTERN`.
- `app/models/jira_issue.py` — `JiraIssue`, `ISSUE_KEY_PATTERN`.
- `app/models/job.py` — `Job`, `JobType`, `JobStatus`, `TERMINAL_STATUSES`.
- `app/models/audit_log.py` — `AuditLog`, `AuditOutcome`, `AuditSource`.
- `app/models/__init__.py` — imports every model, which is what populates
  `Base.metadata` for `--autogenerate`.
- `alembic.ini` — no database URL in it; `env.py` supplies one.
- `migrations/env.py` — blocking engine per ADR-006, URL from
  `app.core.config`, `compare_type` and `compare_server_default` on.
- `migrations/script.py.mako` — Alembic's standard template.
- `migrations/versions/20261108_1dc7e5cdad8b_initial_schema.py` — the first
  revision.
- `docs/decisions/ADR-008-schema-conventions.md` — keys, enums, timestamps,
  constraint names.
- `docs/decisions/ADR-009-what-the-cache-and-audit-trail-store.md` — what
  `jira_issues` and `audit_logs.detail` may hold.

Changed:

- `app/core/database.py` — `Base.metadata` now carries a constraint naming
  convention. This had to happen before the first table existed.
- `README.md` — project structure, and a new "Database migrations" section.
- `migrations/README.md` — rewritten: everyday commands, and the rules for
  writing a revision including what autogenerate misses.
- `CHANGELOG.md` — one line for this step.

`docs/api.md` and `docs/mcp-tools.md` are unchanged. This step adds no
endpoints and no tools; the schema is documented here and in ADR-008/009.

## How it works

### The five tables and why each exists

**`users`** — a person who may use the platform, identified by their work
email. The email is what arrives from Copilot Chat through the MCP server's
actor header, so it is the natural external key. It is stored lowercase, and a
`CHECK` enforces that, because two rows differing only in case would be two
accounts for one person. `role` defaults to `viewer`: a newly created user gets
the least privilege, not the most. Users are deactivated with `is_active`
rather than deleted, so their jobs and audit rows stay attributable.

**`user_project_access`** — one row grants one user read access to one Jira
project. **This is the authorisation check in front of every Jira read.** The
Jira service account can see far more than any individual user should, so
Step 6 looks for a row here before calling Jira. Without it, any authenticated
user could read any issue in the tenant. There is no `updated_at`: a grant is
created or revoked, never edited.

**`jira_issues`** — a read-through cache, not a source of truth.
`fetched_at` is what Step 6 compares against `JIRA_CACHE_TTL_SECONDS`. It is
deliberately separate from `updated_at`: a refetch that returns identical data
still moves `fetched_at`, which is correct for a TTL and wrong for a
row-changed timestamp.

`project_key` is a **stored generated column**,
`split_part(issue_key, '-', 1)`, computed by Postgres rather than written by
the application. That matters more than it looks: the project key is what the
permission check compares against `user_project_access`. If the application
wrote it and ever wrote it wrongly, the result would be an authorisation bug —
a user reading an issue from a project they were never granted. A generated
column cannot drift.

**`jobs`** — one unit of asynchronous work. Phase 1 creates none; the table is
here because the first migration is the cheapest one to get right, and because
Step 7's audit rows and Phase 2's BRD generation both want to reference a job
id. The `CHECK` tying terminal status to `finished_at` is the interesting part:

```
(status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)
```

Without it, a crashed worker leaves rows that look like they are still running
forever, and rows that claim to be finished with no timestamp, and no query can
distinguish the two. Postgres evaluates this on every write, so no bug in a
future worker can break the invariant.

**`audit_logs`** — the record of who asked for what, when, and whether they
were allowed. This is the main reason the MCP server exists as a separate
gatekeeper at all. Three properties follow from that job:

1. *Append-only.* Nothing in this project updates or deletes a row here.
   `occurred_at` is `server_default now()`, so an actor cannot backdate their
   own entry.
2. *Self-contained.* `actor_email` is stored as text next to the nullable
   `actor_user_id` foreign key, which is `ON DELETE SET NULL`. Deleting a user
   nulls the link and leaves the history readable.
3. *No secrets.* `detail` is schemaless JSONB; ADR-009 sets out exactly what
   may go in it.

Its primary key is a `bigint` identity rather than a UUID like every other
table. The table is append-only and read in time order, so a monotonic key
keeps inserts at the right-hand edge of the index. Nothing external quotes an
audit id, so there is no enumeration concern to trade against. ADR-008 has the
reasoning; the model docstring repeats it at the site, because the
inconsistency will otherwise look like an oversight.

### Conventions, all in `app/models/base.py`

Three mixins rather than one base class with everything on it:
`audit_logs` wants a different key type, and `user_project_access` has no
meaningful `updated_at`. Composing small pieces says that; a single
`BaseModel` would not.

`sort_order` on the mixin columns exists for readability only. Without it,
mixin columns are appended after the model's own, so `id` landed at the *end*
of every `CREATE TABLE` and `updated_at` sorted before `created_at`.

Every timestamp is `timestamptz`. A naive `timestamp` column records the
server's local clock, and the first time a container runs in another timezone
the data stops being orderable. The verification asserts that no
`timestamp without time zone` column exists anywhere in the schema.

Enums are `VARCHAR` plus `CHECK`, built by `enum_column()`. See the
`create_constraint` trap in Decisions below — it is the most important thing in
this step.

### Alembic

`migrations/env.py` uses `create_engine`, not `create_async_engine`, per
ADR-006: migrations are a serial batch job, so async buys nothing.

The URL never appears in `alembic.ini`. `sqlalchemy.url` is empty and `env.py`
calls `app.core.database.sync_database_url()`, so there is one place that knows
the credentials and no password in a committed file. `env.py` prints the masked
URL before connecting, which is how you confirm a migration is about to hit the
database you think it is.

`env.py` imports `app.models` purely for the side effect on `Base.metadata`. If
that import is ever removed, `--autogenerate` sees an empty schema and
cheerfully writes a migration that drops all five tables.

`transaction_per_migration=True` wraps each revision in its own transaction, so
a failure halfway through a multi-revision upgrade leaves the database at a
recorded revision rather than somewhere undefined.

## How to run / verify

Note on the commands below: they are bare, with expected output in prose.
Interactive `zsh` does not treat `#` as a comment unless `interactive_comments`
is set, so a trailing comment becomes an argument and produces errors like
`zsh: unknown file attribute`.

The infrastructure must be up, and the commands read `.env`:

```
docker compose up -d postgres redis
```

Apply the migration:

```
uv run alembic upgrade head
```

Expect a masked target line and one `Running upgrade` line:

```
alembic target: postgresql+psycopg://qa:***@localhost:5433/qa_platform
INFO  [alembic.runtime.migration] Running upgrade  -> 1dc7e5cdad8b, Initial schema: users, project access, cached Jira issues, jobs, audit logs.
```

Confirm the recorded revision:

```
uv run alembic current
```

Expect `1dc7e5cdad8b (head)`.

List the tables:

```
docker compose exec -T postgres psql -U qa -d qa_platform -c '\dt'
```

Expect six: `alembic_version` plus the five above. Column counts are
`users` 7, `user_project_access` 5, `jira_issues` 12, `jobs` 14,
`audit_logs` 11, against PostgreSQL 17.11.

Check the models still match the database:

```
uv run alembic check
```

Expect `No new upgrade operations detected`. **Read the warning in Decisions
below before trusting this command.**

Exercise the downgrade, which is the half of a migration nobody tests until
they need it at 2am:

```
uv run alembic downgrade base
uv run alembic upgrade head
```

After the downgrade only `alembic_version` remains; after the upgrade all six
tables are back.

Confirm the enum CHECK constraints really exist, which `alembic check` will not
tell you:

```
docker compose exec -T postgres psql -U qa -d qa_platform -c "select conname, pg_get_constraintdef(oid) from pg_constraint where conname in ('ck_users_user_role','ck_jobs_job_status','ck_jobs_job_type','ck_audit_logs_audit_outcome','ck_audit_logs_audit_source');"
```

Expect five rows, each a `CHECK (... = ANY (ARRAY[...]))`. Zero rows means the
`create_constraint` regression described below has come back.

Lint, format and tests:

```
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Expect `All checks passed!`, `63 files already formatted`, and `no tests ran`.

Behaviour was verified by a throwaway script covering 40 assertions: an ORM
round trip through all five tables, 14 constraint-rejection cases, the four
`ON DELETE` behaviours, `lazy="raise"`, and five catalog facts. See Known
issues — it is not a committed test.

## Decisions and trade-offs

**`create_constraint=True` is mandatory on every non-native enum, and this was
found the hard way.** `native_enum=False` alone produces a plain `VARCHAR` with
no validation at all; SQLAlchemy has defaulted `create_constraint` to `False`
since 1.4. The first version of this schema accepted `role='superuser'` and
`outcome='maybe'` without complaint. The whole argument for choosing
`VARCHAR` + `CHECK` over a native Postgres `ENUM` is the `CHECK`, so the
schema had exactly the downside of the decision and none of the benefit.
Fixed in `enum_column()`, which now sets both flags, with a comment saying why.

**`alembic check` does not compare CHECK constraints.** It reported "No new
upgrade operations detected" both before and after that fix. This is the more
general lesson: a green `alembic check` means added, removed and retyped
*columns, tables and indexes* match. It says nothing about `CHECK` constraints,
and by default nothing about types or server defaults either — `compare_type`
and `compare_server_default` are off unless set, and `env.py` sets them. Any
change to a `CHECK` needs a hand-written migration and a catalog query to
confirm.

**UUID keys except for `audit_logs`.** ADR-008, with the alternatives. UUIDv7
would be the better answer and is deliberately deferred: Postgres 17 has no
built-in generator, and hand-rolling one would give up the `server_default`
that lets `psql` and data migrations work.

**`project_key` is generated by the database, not the application.** It feeds
an authorisation check, so a value that can drift from `issue_key` is an
authorisation bug waiting to happen.

**The constraint naming convention was added before the first table.** Changing
it later renames every constraint in the database. Without it, autogenerate
emits `op.create_unique_constraint(None, ...)` and a later migration that wants
to drop that constraint cannot name it — the downgrade is unwritable.

**`updated_at` is ORM-maintained, not trigger-maintained.** A trigger is
correct in more cases but is DDL that autogenerate does not manage, so it would
live only in a hand-written migration and every later `alembic check` would be
silent about it — the same class of invisible drift as the `create_constraint`
bug. The cost is that `updated_at` is stale after a raw SQL `UPDATE`. ADR-008
records the trade; revisit if anything starts writing these tables outside the
ORM.

**`jobs` and the full `JobType` enum exist although Phase 1 creates no jobs.**
Listing the later phases' job types now means the `CHECK` does not need
widening three times. Writing the table later would mean migrating a table that
other things already reference.

**No raw Jira payload column.** ADR-009. A Jira issue carries reporters,
assignees, comments, attachments and arbitrary custom fields — personal data
for people who are not users of this platform — and a cache table is the last
place anyone looks when answering a deletion request. The cost is a migration
and a re-fetch if a later phase needs another field.

**Migrations are not run automatically on application startup.** Nothing in
`app/main.py` calls Alembic. Auto-migrating on boot means N replicas racing to
run the same DDL, and a bad migration taking the service down rather than
failing a deploy step. Running `alembic upgrade head` is a deliberate act.

## Security notes

- **No credential in any committed file.** `alembic.ini` has an empty
  `sqlalchemy.url` with a comment saying why; the URL comes from
  `app.core.config`. Verified that the only URL `env.py` prints is
  `database_url_safe`, with the password replaced by `***`.
- **`user_project_access` is the authorisation boundary for Jira reads.** The
  service account's token can see the whole tenant, so this table is what stops
  a user borrowing that reach. Step 6 must check it *before* calling Jira, not
  after.
- **`project_key` cannot be forged.** It is a stored generated column. The
  verification confirms Postgres refuses an INSERT that tries to write it
  directly, so the value the permission check reads is always derived from the
  issue key actually requested.
- **The audit trail survives user deletion.** `actor_user_id` is
  `ON DELETE SET NULL` and `actor_email` is stored as text. Verified by
  deleting a user and confirming the audit row remains with its email intact
  and the foreign key nulled. `jobs.requested_by_email` behaves the same way.
  Conversely `user_project_access` *does* cascade: deleting a user revokes
  their access, which is the safe direction.
- **Audit rows cannot be backdated.** `occurred_at` is `server_default now()`,
  set by the database rather than by the caller.
- **`audit_logs.detail` and `jobs.params`/`jobs.result` are schemaless JSONB
  and are the most likely place a secret will eventually be written.** ADR-009
  states the rule — no tokens, no credentials, no `Authorization` header, no
  request or response bodies, no verbatim user input, nothing unbounded. This
  is enforced by review, not by code; see Known issues.
- **Enum values are now constrained at the database level.** Before the fix
  described above, any string was accepted — including in `audit_logs.outcome`,
  where a bogus value would have corrupted the "show me every denial" query
  that makes the audit trail useful.
- **`users.role` defaults to `viewer`.** A row created without an explicit role
  gets the least privilege.
- **No model `__repr__` prints anything beyond an identifier.** `User.__repr__`
  returns `<User eng@acme-qa.co>` and nothing else, so a model accidentally
  interpolated into a log line or a traceback does not dump the row. Asserted
  in verification.

## Known issues / TODO

- **Still no committed tests.** The 40 assertions behind this step ran from a
  scratchpad script, as in Steps 3 and 4. Three steps of untested,
  security-relevant behaviour have now accumulated: the `ON DELETE` semantics
  of the audit trail and the generated `project_key` are both *authorisation*
  properties with no regression test. Suggested:
  `tests/integration/test_schema_constraints.py`.
- **`audit_logs.detail` has no schema,** so ADR-009's rules about what may not
  be stored are enforced by review alone. The obvious fix is a Pydantic model
  per action, which is premature with one action but should happen before there
  are several.
- **`audit_logs` grows without bound.** No retention policy. One row per MCP
  tool call makes this slow, not urgent, but a decision is owed before any
  deployment — and it interacts with ADR-009's claim that a deletion request is
  answerable.
- **The append-only rule is a convention, not a grant.** Nothing stops a future
  `UPDATE audit_logs`. A separate database role with `INSERT` and `SELECT` only
  would make it structural.
- **`ISSUE_KEY_PATTERN` and `PROJECT_KEY_PATTERN` are duplicated** between the
  model `CHECK` constraints and, from Step 7, the MCP tool's validation. They
  must agree. The models export both constants so the MCP server can import
  rather than retype them; whoever writes Step 7 should do that.
- **`updated_at` is stale after a raw SQL `UPDATE`.** See Decisions.
- **No `users` or `user_project_access` rows exist,** so Step 6 cannot
  authorise anybody yet. A seed path is needed — a small CLI, an admin
  endpoint, or a documented `psql` snippet. Nothing in the brief covers it, so
  it needs a decision rather than an assumption.
- **No test database.** Verification ran against the development database and
  deletes its own rows afterwards. Step 8 will need a separate database or
  per-test transaction rollback; sharing one is how a test suite starts
  deleting a developer's data.
- **`mcp` service still cannot start** (needs Step 7), so
  `docker compose up -d api` remains the working command.
- **No mypy.** Carried over from Step 3.

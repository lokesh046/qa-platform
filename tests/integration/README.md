# tests/integration

Empty on purpose, as of Step 8.

## Why nothing is here yet

Everything in `tests/unit` runs with no database, no Redis and no network, so
`uv run pytest` passes on a clean checkout with nothing running. That is worth
protecting: a suite that needs `docker compose up` is a suite people stop
running.

An integration test, by definition, gives that up. Before writing the first
one, this directory needs a fixture that:

1. creates a **separate** database, not `qa_platform` — the Step 1-7
   verification harnesses seeded rows into the development database and
   cleaned up after themselves, which worked but is not something to repeat on
   purpose;
2. runs `alembic upgrade head` against it once per session;
3. wraps each test in a transaction and rolls it back, so tests do not see
   each other's rows.

## What belongs here once that exists

Things the unit tests deliberately cannot reach, in rough order of value:

- **`app/cli.py`'s commands.** Idempotency (`grant-access` twice), the
  lowercase-email CHECK, and the `uq_user_project_access_user_id_project_key`
  unique constraint. `tests/unit/test_cli.py` covers only argument parsing.
- **`app/modules/audit/service.record`.** It opens its **own** session and
  commits it, which is what makes a denial survive the exception that reports
  it (ADR-013). A unit test replaces it with a list, so the thing that matters
  — that the row is still there after a rollback — is untested.
- **`repository.upsert_issue`.** The `ON CONFLICT DO UPDATE`, and that
  `project_key` is left to the generated column. A Python-level stub cannot
  show that Postgres accepts the statement.
- **The enum CHECK constraints.** Step 5 found that `native_enum=False` alone
  creates no constraint, and that `alembic check` does not compare CHECK
  constraints. Only a real `INSERT` of `role='superuser'` proves the fix.
- **`GET /api/v1/ready`.** The 503 path with a dependency genuinely down.
- **The endpoints end to end**, through `httpx.ASGITransport`, including the
  401 and 403 paths that `require_actor` produces.

## Running the API's HTTP surface without a database

Some endpoint tests do not need one. `GET /api/v1/health` touches no
dependency by design (ADR-007), and the error envelope and request-id
middleware can be exercised against a throwaway `FastAPI` app. Those could
live in `tests/unit` instead; they are listed here so the distinction stays
deliberate rather than accidental.

## A defect this directory would have caught

Step 12 shipped a `MissingGreenlet` crash in `app/modules/brd/repository.py`
that 591 unit tests missed. `brd_documents.project_key` is a `Computed` column
and `created_at` is a server default, so SQLAlchemy expires both after every
INSERT **and** every UPDATE; reading one afterwards triggers a lazy load, which
async SQLAlchemy refuses. The unit tests stub the repository and build a
detached object with every attribute set in Python, so the expiry never
happens.

The test that catches it is three lines against a real session:

```python
document = await repository.open_attempt(session, issue_key="SCRUM-1", ...)
await repository.complete_attempt(session, document, markdown="...", ...)
assert document.project_key == "SCRUM"   # raises MissingGreenlet without the fix
```

Which is the general shape of what belongs here: assertions about what the
*database* does, made against the database.

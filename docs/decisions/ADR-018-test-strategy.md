# ADR-018: The committed test suite needs no infrastructure

Date: 2026-11-08
Status: Accepted

## Context

Steps 1 to 7 were each verified by a script in a scratch directory: 319
assertions in six files. They found real bugs — the missing enum CHECK
constraints, the `create_app(settings=...)` parameter that did nothing, the
discarded `ToolError` messages — so the approach worked. But none of them
could be committed, for one reason: they ran against the development database.
They connected to `qa_platform`, inserted users and grants, and deleted them
afterwards.

That is acceptable for a script run once by the person who wrote it. It is not
acceptable for `uv run pytest`:

- It requires `docker compose up` before the suite can run at all, and a suite
  that needs a prerequisite is a suite people stop running.
- It writes to the database a developer is also using. The cleanup is correct
  until a test fails halfway through.
- It reads the developer's `.env`, so the same test can pass on one machine
  and fail on another — and the Step 6 harness proved that is not theoretical,
  by reaching the real `*.atlassian.net` wildcard DNS through a placeholder
  hostname.

Step 8's job is the committed suite, so the question is what it is allowed to
depend on.

## Decision

Everything under `tests/unit` runs with **no database, no Redis, no network and
no `.env`**. `uv run pytest` passes on a clean checkout with nothing running.

Four mechanisms deliver that:

1. **A hermetic environment.** `tests/conftest.py` has an autouse fixture that
   deletes all 24 environment variables either settings class reads, and
   every settings object is built with `_env_file=None`. A test that needs a
   value sets it itself, which puts the dependency in the test rather than in
   whoever's shell runs it.
2. **Jira and the backend are mocked with `respx`.** No hostname is resolved
   and no socket is opened. Each status the real services can return gets a
   case, which is how a wrong status mapping is caught — the alternative is
   finding out on the day Jira returns a 429.
3. **The jira service's repository is stubbed, and the stub records the order
   it was called in.** The order of operations in
   `app/modules/jira/service.py` is that module's security property, so
   "no Jira call was made before the project-access check passed" is asserted
   against an observed call list rather than reviewed by eye.
4. **The MCP tool is driven through the SDK's in-process `Client`.** Not by
   calling the handler function. Two properties only exist on the far side of
   the SDK: that a message reaches the model at all, and what the model is
   told about the tool's schema and description.

`tests/integration` stays empty, with a README listing what belongs there and
the fixture it needs first: a separate database, one `alembic upgrade head`,
and a transaction rolled back per test.

## Alternatives

- **Commit the harnesses as they are.** Fastest, and it would have shown a test
  count immediately. Rejected for the reasons above; it also keeps the `.env`
  dependency, which is the part that makes a failure hard to reproduce.
- **An in-memory SQLite database for the unit tests.** Tempting and common.
  Rejected because this schema is not portable: it uses a `Computed` generated
  column with `split_part`, JSONB, a partial index with a `WHERE` clause, and
  `INSERT ... ON CONFLICT`. A suite passing against SQLite would prove the code
  works against a database this project will never use, and the places most
  likely to break are exactly the Postgres-specific ones.
- **Testcontainers, starting a real Postgres per session.** The right answer
  for the integration tests, and the README says so. Rejected as the basis for
  the *unit* suite: it needs Docker running, takes seconds rather than
  milliseconds, and would make the fast feedback loop depend on the slow one.
- **Mock at the HTTP boundary only**, driving the API with `ASGITransport` and
  faking nothing else. Rejected because it needs the database for any endpoint
  that reads one, which is all of them except `/health`.
- **Test the service by calling the repository's real functions against a
  stubbed `AsyncSession`.** Rejected: faking a session means faking
  `execute`, result objects and `scalar_one_or_none`, which is a lot of
  machinery that asserts SQLAlchemy's behaviour rather than ours.

## Consequences

- 433 assertions run in about one second, with nothing started.
- Several things are consequently **untested**, and the list is in
  `tests/integration/README.md` rather than implied: `audit.service.record`'s
  own-session commit, `upsert_issue`'s `ON CONFLICT`, the enum CHECK
  constraints, the CLI's commands, and the endpoints' 401 and 403 paths. The
  scratch harnesses covered some of these; they will not survive the session,
  so those gaps are real and recorded in the Step 8 log.
- A stubbed repository can drift from the real one. Nothing stops
  `FakeRepository.get_user_by_email` keeping a signature the real function no
  longer has. An integration test is what catches that, which is another
  reason the directory should not stay empty.
- The suite cannot catch anything about the schema. Step 5 already established
  that `alembic check` does not compare CHECK constraints either, so schema
  verification currently has no automated cover at all.
- `pyproject.toml` relaxes six lint rules under `tests/**`, each with a reason
  in the comment. `ARG001` is the one worth knowing about: a pytest fixture
  requested for its side effect looks unused to the linter.

# Step 08: The committed test suite

Date: 2026-11-08
Phase: 1

## Goal

Write the unit tests the brief asks for — argument validation and the jira
service with Jira mocked — as a committed suite that runs with no database, no
Redis and no network. This closes the gap flagged at the end of every step
since Step 3: 319 assertions existed only as scripts in a scratch directory
that will not survive the session.

## What was done

### Tests

| Path | Why |
| --- | --- |
| `tests/conftest.py` | Hermetic fixtures. An autouse fixture deletes all 24 environment variables this project reads; settings are built with `_env_file=None`. Also `log_buffer`, which captures *rendered* log output for the secret-hygiene assertions. |
| `tests/unit/test_adf.py` | 51 tests for `app/modules/jira/adf.py`. Pure functions parsing untrusted input, so the cheapest tests here and the ones most worth having. Covers the three shapes Jira returns, every node type, malformed children, and both bounds (depth and output size). |
| `tests/unit/test_validation.py` | 141 tests. The issue-key pattern against a table of 21 malformed keys, run through **both** validators plus a test asserting the two agree; `extra="forbid"`; the `mcp.` audit namespace; non-scalar `detail`; and every configuration validator in both settings classes. |
| `tests/unit/test_jira_client.py` | 31 tests for the Jira gateway, with `respx`. One case per status branch, plus non-JSON, wrong-shaped JSON, an oversized body, a timeout and a connection failure. |
| `tests/unit/test_jira_service.py` | 57 tests for the four gates. The stub repository **records the order it was called in**, so "no Jira call before project access passed" is an assertion rather than a code review. |
| `tests/unit/test_mcp_auth.py` | 29 tests for the two transports' trust models, including 11 http refusals each checked for which message it gives. |
| `tests/unit/test_mcp_backend_client.py` | 38 tests. A case per status, which is the regression test for the Step 7 bug described below. |
| `tests/unit/test_mcp_tool.py` | 49 tests driving the tool through the SDK's in-process `Client`, which is the only way to assert that a message reaches the model at all. |
| `tests/unit/test_cli.py` | 36 tests for argument parsing and normalisation — everything that happens before the CLI opens a connection. |
| `tests/integration/README.md` | Why the directory is empty, the fixture it needs first, and what belongs in it. |

### Product changes

Three of these are fixes for things the tests found.

| Path | Change |
| --- | --- |
| `app/core/config.py` | New `format_validation_error`. Renders a settings failure from the field name and the rule, never the value. See ADR-016. |
| `app/main.py` | `create_app` catches `ValidationError` and raises `RuntimeError` with the redacted text, `from None` so the original is not printed either. |
| `mcp_server/server.py` | `main()` prints the redacted text instead of `str(exc)`. |
| `app/core/security.py` | `parse_actor` lowercases the actor header. See ADR-017. |
| `app/modules/audit/schemas.py` | `AuditEvent` validator canonicalising `actor_email`, covering rows written from outside an HTTP request. |
| `pyproject.toml` | Six lint rules relaxed under `tests/**`, each with its reason in a comment. |

### Decision records

- ADR-016: configuration errors are redacted before they are printed.
- ADR-017: one canonical form for an actor's email.
- ADR-018: the committed test suite needs no infrastructure.

## How it works

### Nothing is started

Four mechanisms, described in full in ADR-018:

- **No ambient configuration.** The autouse fixture in `tests/conftest.py`
  empties the environment of everything this project reads, and
  `_env_file=None` stops `Settings` falling back to `.env`. Without both, a
  test would inherit the developer's real Jira host — which is how the Step 6
  harness ended up making real HTTPS requests to Atlassian through a
  placeholder hostname.
- **Jira and the backend are `respx` mocks.** No DNS, no sockets.
- **The jira service's repository is a stub** that appends to a `calls` list.
  So the module's security property is checked by observation:

  ```python
  repo.has_access = False
  with pytest.raises(NotFoundError):
      await service.get_issue(FakeSession(), ACTOR, "ABC-123", settings=settings)
  assert route.call_count == 0  # Jira was never called
  assert repo.calls == ["get_user_by_email", "has_project_access:ABC"]
  ```

  The second assertion is the stronger one: the cache was not even consulted.

- **The MCP tool runs over the SDK's in-process transport**, so the assertions
  see what a Copilot Chat client would see — including whether an error message
  survived the boundary.

### Why the MCP tests do not use a fixture for the client

They use an `@asynccontextmanager` helper entered inside each test body.
pytest-asyncio runs an async fixture's setup and teardown in two different
tasks, while the SDK's client holds an anyio cancel scope that must be exited
in the task that entered it. A fixture version raises "Attempted to exit cancel
scope in a different task" as every test using it tears down — 46 errors with
every assertion passing, which is a confusing way to learn this.

### What a stub cannot tell you

`audit.service.record` opens its **own** session and commits it, which is what
makes a denial survive the exception that reports it (ADR-013). The unit tests
replace it with a list, so they verify that an event *was* recorded and what it
contained — not that the row is still there after the request's transaction
rolls back. That needs a database, and it is first on the list in
`tests/integration/README.md`.

## How to run / verify

```bash
# The whole suite. Nothing needs to be running.
uv run pytest

# Expected:
# 433 passed in 1.03s

# One file, verbosely
uv run pytest tests/unit/test_jira_service.py -v

# Lint and format
uv run ruff check .          # All checks passed!
uv run ruff format --check . # 101 files already formatted
```

Confirm the suite really is hermetic by taking everything away:

```bash
docker compose down
mv .env .env.hidden && uv run pytest; mv .env.hidden .env
# 433 passed
```

Verify the two redaction fixes behave in the real processes:

```bash
# Backend: the password must not appear.
env DATABASE_URL='postgresql://qa:sup3rs3cret@localhost:5433/qa_platform' \
  uv run python -c "import app.main"
# RuntimeError: The backend cannot start: its configuration is not valid.
#   DATABASE_URL: must use the 'postgresql+psycopg' scheme, got 'postgresql'

# MCP server: the token must not appear, and the exit code must be 2.
env INTERNAL_API_TOKEN='tok3n-too-short' uv run python -m mcp_server.server; echo $?
# mcp server configuration error:
#   INTERNAL_API_TOKEN: is 15 characters; at least 32 are required. ...
# 2
```

Verify the actor canonicalisation against the running stack:

```bash
curl -s -o /dev/null -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
     -H "X-Actor-Email: QA.One@Acme-QA.co" \
     http://127.0.0.1:8000/api/v1/issues/ABC-999

docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select actor_email from audit_logs order by id desc limit 1;"
#    actor_email
# -------------------
#  qa.one@acme-qa.co      <- not QA.One@acme-qa.co
```

## Decisions and trade-offs

**No SQLite for the unit tests.** The obvious way to make a suite
database-independent, and wrong here: the schema uses a `Computed` generated
column with `split_part`, JSONB, a partial index and `INSERT ... ON CONFLICT`.
A suite passing against SQLite would prove the code works against a database
this project will never use, and the Postgres-specific parts are exactly the
ones most likely to break. ADR-018 has the full comparison.

**A stubbed repository rather than a stubbed session.** Faking an
`AsyncSession` means faking `execute`, result objects and
`scalar_one_or_none`, which asserts SQLAlchemy's behaviour rather than ours.
Stubbing the four repository functions keeps the test about the service.

**Error messages are asserted as text.** Usually a smell. Here the string is
the contract: `mcp_server/backend_client.py` exists to turn a status into a
sentence a language model shows to a person, so "502 Bad Gateway" failing to
appear is the thing worth testing.

**One case per HTTP status, not one per interesting status.** More verbose, and
it is what catches the class of bug Step 7 hit: a `_user_message` branch that
is only reached by some statuses can be wrong for months. There is also a test
asserting that *no* status produces anything other than a `BackendError`,
because the caller's audit branch catches only that type.

**A test that asserts a raw `ValidationError` does leak the value.** Kept
deliberately, in `test_validation.py`. It documents why
`format_validation_error` exists, so nobody deletes the helper as ceremony.

**The `audit` route fixture is autouse.** Every tool call writes a record, so a
test that left the route unregistered would exercise the fail-closed path by
accident — which is what the first draft of `test_mcp_tool.py` did. `respx`
answered the unregistered POST with a 200, `post_audit` requires a 201, and the
resulting failures looked like bugs in the tool.

**Six lint rules relaxed under `tests/**`.** `S101` (assert), `D103` (a test's
name is its description), `ANN201` (every test returns `None`), `ARG001` (a
fixture requested for its side effect looks unused), and `S105`/`S106` (every
credential in a test is fake by construction). The real guard against a secret
in the suite is that nothing in it reads `.env`.

## Security notes

- **A config error no longer prints the credential it rejected.** The worse of
  the two paths was the backend's: `app = create_app()` runs at import, so a
  `ValidationError` reached uvicorn, which printed `DATABASE_URL` complete with
  its password into the container log. ADR-016 has the detail. The fix requires
  that a validator's own message not quote a secret either; the existing ones
  report a scheme, a length or a field id.
- **One person is now one actor in the audit trail.** `EmailStr` lowercases the
  domain but not the local part, so the same user could be recorded as
  `qa.one@`, `QA.One@` and `Qa.oNe@` — splitting a run of denials, which is
  exactly what the trail exists to show, across what look like three people.
  `users.email` has a CHECK constraint permitting only the lowercase form, so
  nothing is lost by canonicalising. ADR-017.
- **No credential appears anywhere in the suite's output.** Asserted, not
  assumed: `test_jira_client.py`, `test_mcp_auth.py` and
  `test_mcp_backend_client.py` each check that the token appears in no log
  line, no error message and no URL. `test_mcp_auth.py` additionally checks
  that a rejected token is logged without any part of it — not even its
  length, which would narrow a guess.
- **The tokens in the tests are fake and local to `tests/conftest.py`.** They
  satisfy the 32-character minimum and nothing else. No test reads `.env`, so
  a real credential cannot reach an assertion or a failure message.
- **`test_mcp_tool.py` asserts that an internal fault does not leak its
  message to the model.** The stub raises `RuntimeError("connection string
  postgresql://qa:hunter2@db/qa failed")` and the test checks `hunter2` is not
  in what the client receives. Exception text routinely contains paths, SQL
  fragments and occasionally connection strings.
- **The malformed-key table includes `ABC-1;DROP TABLE users`**, and a test
  asserts it is not stored verbatim in the audit detail. Not because SQL
  injection is plausible here — every query is parameterised — but because the
  value is model-supplied free text and ADR-009 forbids storing that.

## Known issues / TODO

- **`tests/integration` is empty.** The things it must cover are listed in its
  README; the highest-value ones are `audit.service.record`'s own-session
  commit, `upsert_issue`'s `ON CONFLICT`, the enum CHECK constraints and the
  CLI's commands. The scratch harnesses covered some of this and will not
  survive the session, so these are real gaps.
- **No schema verification at all.** Step 5 established that `alembic check`
  does not compare CHECK constraints, and the unit suite cannot see the
  database. A migration that drops a constraint would pass everything.
- **The stub repository can drift from the real one.** Nothing checks that
  `FakeRepository.get_user_by_email` still matches the function it replaces.
- **No coverage measurement.** `pytest-cov` is not a dependency. Worth adding
  with a threshold once the integration tests exist; adding it now would
  measure a suite that deliberately excludes the database layer.
- **No CI.** The suite needs nothing, which makes a GitHub Actions workflow
  about ten lines. There is no git repository yet either.
- **No mypy.** Three `# type: ignore` comments exist and nothing checks them.
- **`app/cli.py`'s commands, `app/modules/health/service.py` and
  `app/core/errors.py`'s handlers have no committed tests.** The error
  envelope and the request-id middleware could be tested without a database
  against a throwaway `FastAPI` app; the health service's readiness probe
  could be tested with both probes stubbed. Neither is in the brief for this
  step, and both are listed in the integration README so the distinction stays
  deliberate.
- **The suite does not test the Docker images.** `docker compose up` working is
  still verified by hand.

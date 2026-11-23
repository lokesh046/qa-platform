# Step 13: The data model for the whole assistant

Date: 2026-11-23
Phase: 2

## Goal

Build the tables the revised brief names, so every later step has somewhere to
put its results: Jira connections, documents with versions and review state,
the requirements and scenarios parsed out of them, proposed Jira work, test
cases, generated scripts, and recorded human decisions.

## What was done

| Path | Why |
| --- | --- |
| `app/models/jira_connection.py` | One row per person per Jira site. Encrypted refresh token, key version for rotation, granted scopes. |
| `app/models/document.py` | `documents` (BRD or FDS, versioned, with a `git_commit`), plus `requirements`, `scenarios` and `open_questions` parsed out of them. |
| `app/models/delivery.py` | `jira_items` (story/task/subtask, with an idempotency key), `test_cases`, `scripts`. |
| `app/models/approval.py` | Append-only record of who decided what, on which version. |
| `migrations/versions/…_c7f6ddfd4f45_…` | Nine tables, 37 CHECK constraints. Round-trips through `downgrade` and `upgrade`. |
| `tests/unit/test_models_schema.py` | 53 tests: every table registered, every enum's values, and each rule-carrying constraint named individually. |
| `app/models/brd_document.py` | Marked **deprecated**, with its removal scheduled for build step 5. |
| `pyproject.toml` | Ruff now skips `.claude/`, which holds vendored third-party skills. |
| `docs/decisions/ADR-025-…` | The agreed architecture, and which earlier ADRs it replaces. |

## How it works

### Versions, not edits

A document is never modified. `lineage_id` groups every version of one
artifact and `version` counts them, with a unique constraint on the pair. "The
current BRD" is the newest approved row for a lineage — a query, not a flag
two writers could both set. What somebody approved stays readable exactly as
they approved it.

### The row indexes what Git holds

The text lives in a commit; `git_commit` names it. Git answers "what changed";
the row answers "which version is approved, by whom, derived from what", which
Git answers badly. A `CHECK` requires a **full** forty-character sha, because
a short sha is ambiguous and an ambiguous reference to approved text is
worthless.

### Three rules the database enforces, not the code

- **An FDS must name the BRD it came from, and a BRD must not have one.** One
  constraint, written as an equality between two booleans, so neither half can
  be forgotten.
- **A subtask must have a parent and a story must not.** A malformed hierarchy
  cannot reach Jira.
- **`status = 'created'` and having a `jira_key` are the same fact.** So "did
  we actually write this to Jira?" cannot have two answers.

Plus: a rejection must carry a note, an answered open question must have all
three of answer, answerer and time, and a failed lint must say why.

### The idempotency key is a unique index

`jira_items.idempotency_key` and `test_cases.idempotency_key` are unique
across their tables. That is the whole guarantee: a retry after a timeout
cannot create a second Jira issue, because the second insert is refused by
Postgres rather than by a code path somebody has to remember.

### Nothing stores a secret except the encrypted refresh token

`refresh_token_encrypted` is `LargeBinary` — bytes, so no encoding step can
lose one and a careless `str()` produces nothing that looks readable. The
access token is never stored; it lives in memory for one request. A test
asserts no table anywhere has a column named `access_token`, `client_secret`,
`password` or `api_token`.

## How to run / verify

```bash
uv run pytest -q          # 644 passed
uv run ruff check .       # All checks passed!
uv run alembic upgrade head
uv run alembic check      # No new upgrade operations detected.
```

The migration round-trips:

```bash
uv run alembic downgrade -1 && uv run alembic upgrade head
```

The constraints were verified by **trying to break each one**, because Step 5
established that `alembic check` does not compare CHECK constraints:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
-- each of these must be refused
INSERT INTO documents (document_type, lineage_id, project_key, source_text, author_email)
VALUES ('fds','33333333-3333-3333-3333-333333333333','SCRUM','x','a@b.co');
INSERT INTO jira_items (document_id, kind, summary, idempotency_key)
VALUES ('<a document id>','subtask','orphan subtask','k1');
INSERT INTO approvals (target_type, target_id, target_version, decision, actor_email)
VALUES ('document','<a document id>',1,'rejected','a@b.co');
SQL
# refused: "ck_documents_fds_has_source"
# refused: "ck_jira_items_only_stories_are_roots"
# refused: "ck_approvals_refusal_has_a_note"
```

All eight rule-carrying constraints tested this way refused. A valid insert
succeeds and the database fills the defaults:

```
 document_type | version | status | project_key
 brd           |       1 | draft  | SCRUM
```

## Decisions and trade-offs

**`documents` was added alongside `brd_documents` rather than replacing it.**
`brd_documents` is live and covered by passing tests, and the `brd` module
that uses it is rewritten in step 5 anyway. Renaming now would break the one
generation path that works, to save a rename later. The cost is two tables for
one idea for the length of one step, which is exactly the smell worth naming:
`brd_document.py` says **deprecated**, says why, and says when it goes.

**`approvals.target_id` is not a foreign key.** Approvals point at five
different tables. A constraint per table would mean five nullable columns of
which exactly one may be set, which is more machinery than it buys. The
integrity that matters — that a decision names a *version* — is a plain
`CHECK`.

**Approvals are append-only, with no `updated_at`.** Changing your mind writes
another decision. A mutable approval row cannot answer "what was agreed on
Tuesday", which is the only question it exists to answer.

**Steps, test data and scenario text are stored as text, not as rows.** A
reviewer approves prose; splitting it into structured rows would mean the text
somebody approved and the text stored could differ. The structure that *is*
extracted — requirements and scenarios — is extracted because everything
downstream cites a requirement id.

**`user_project_access` stays, for now.** It is redundant once Jira enforces
permissions in step 4, and removing it today would leave `get_jira_issue` with
no access check at all.

## Security notes

- **The refresh token is the only credential stored, and only as ciphertext.**
  The column is bytes; the model cannot decrypt it; no query returns it. A
  `key_version` column exists so a key can be rotated without guessing which
  rows are on the old one.
- **A test asserts no table has a secret-shaped column.** `access_token`,
  `client_secret`, `password`, `api_token` — if one appears, the suite fails.
- **Idempotency is enforced by a unique index, not by a code path.** The
  defence against writing a duplicate issue into somebody's Jira does not
  depend on a retry handler being correct.
- **Every email column is lowercase by `CHECK`**, so ADR-017's "one person is
  one actor" holds in each new table even for a row written by something that
  forgets.
- **`CASCADE` is used only downward within a document** (requirements,
  scenarios, questions, items). Approvals and audit rows are never cascaded:
  deleting a user sets their id to null and keeps the record, because the
  record is evidence.
- **No new secret in configuration yet.** The key that encrypts refresh tokens
  arrives with step 4, which is where it is used.

## Known issues / TODO

- **The CHECK constraints are verified by hand, not by the suite.** The unit
  tests assert each constraint is still *declared*; proving it still *works*
  needs a database. Fourth step in a row where this gap is the limiting
  factor.
- **Two tables for one idea** until step 5 removes `brd_documents`.
- **Nothing writes to any of these tables yet.** Nine tables and no service —
  deliberate, since every later step needs them, but it means the shapes are
  unproven against real use.
- **No encryption helper exists**, so `refresh_token_encrypted` has no way to
  be filled. That is step 4's first task, with its key in configuration.
- **`approvals.target_id` can point at a row that does not exist.** Nothing
  enforces it; a service bug could record a decision about nothing.
- **The `position` columns have no uniqueness.** Two requirements can share a
  position and the order between them is then undefined.

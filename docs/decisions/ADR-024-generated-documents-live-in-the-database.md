# ADR-024: A generated BRD is a database row, not a file

Date: 2026-11-09
Status: Accepted

## Context

ADR-019 deliberately left open where generated BRDs are stored. The Step 1
scaffold had an intention, written in `specs/openspec/README.md` before that
directory was deleted:

> Generated BRDs land here as files so they can be reviewed in a pull request
> rather than only existing as rows in a database.

The goal is right — a BRD is a document people read and argue about, and a diff
is a good way to argue about a document. The mechanism does not survive
contact with how this platform runs:

- **The API's filesystem is ephemeral.** It is a container. A file written
  during a request is gone on the next deploy, and on a second replica it was
  never there.
- **Writing into the repository means the API commits to git.** It would need a
  git identity, a push credential, a branch strategy and a way to handle two
  requests generating at once. An API server that pushes commits is a large
  amount of new risk for a reviewing convenience.
- **The row has to exist anyway.** Phase 3 generates tests *from* a BRD, and
  needs to know which issue it belongs to, who asked for it, which prompt and
  model produced it, and whether a person approved it. That is a row whatever
  else happens.

## Decision

**`brd_documents.markdown` is the source of truth.** One row per generation
attempt, holding the document and everything known about how it was produced.

- The markdown is `TEXT`, bounded before it is stored: the validator refuses a
  reply over 60 000 characters (`MAX_DOCUMENT_CHARS`), so a row cannot be
  unbounded even though the column is.
- A row records `issue_key`, the generating actor, `prompt_version`,
  `model_reported`, the token counts, the requirement and scenario counts, and
  a status.
- **Regeneration appends.** No unique constraint on `issue_key`: a BRD written
  against a ticket as it was last Tuesday is evidence of what was agreed then,
  and overwriting it loses that. "The current BRD" is the newest `ready` row
  for the issue.
- **Exporting to a file is a separate, explicit action**, for a later step, and
  never a side effect of generating. The platform can hand somebody a `.md`
  file to commit; it does not commit one itself.

## Alternatives

- **Write files into the repository working tree, as Step 1 intended.**
  Rejected for the three reasons above. The reviewing benefit is real, and
  point four keeps it reachable without putting a git client in a web request.
- **Object storage (S3 or compatible).** The natural home for a large
  immutable artifact, and ADR-005 already deferred choosing a provider until
  Phase 3 needs one for Playwright traces and video. Rejected here: a 20 KB
  markdown document is not that artifact, and it would mean a row *and* an
  object that can disagree, plus a provider decision this step does not need to
  make. Phase 3's screenshots are a better reason to make it.
- **A mounted volume shared by the API replicas.** Works, and avoids the
  ephemeral-filesystem problem. Rejected: it needs its own backup, its own
  consistency story with the rows that point into it, and a second place to
  look when a document is missing — for a document small enough to live in the
  row.
- **Store only the parsed structure (requirements and scenarios as JSONB) and
  render markdown on demand.** Tempting, and better for querying "every
  scenario mentioning passwords". Rejected for Phase 2: the markdown is what a
  person approves, and a renderer would mean the approved text and the stored
  text could differ. The structure is stored *alongside* as counts now, and
  JSONB can be added later without losing the text.

## Consequences

- **Documents are in the database backup.** `pg_dump` now carries ticket-derived
  prose, which is a change in what a database backup contains. ADR-009 limited
  what the *cache* stores; this is a deliberate exception for a document the
  platform authored, and it is the reason the retention question below matters.
- **The table grows with every regeneration** and nothing prunes it. There is
  no retention policy, and a prompt-tuning session on one ticket can produce
  twenty rows. Worth a policy before this is used in anger; not worth inventing
  one before the first real document exists.
- **"The current BRD" is a query, not a column.** Newest `ready` row for the
  issue. Every caller must express that the same way, which is what the
  repository function is for — the alternative is a `is_current` flag that two
  writers can both set.
- **Reviewing a BRD in a pull request is no longer free.** It needs an export
  step somebody has to run. That may never be built, because Phase 3's
  `approvals` module approves documents *in the platform*, with the decision
  recorded in the audit trail — which is a better answer than a merge commit
  for "who signed this off".
- **A failed generation is a row too.** It records the reason, which makes
  "how often does the model return something unusable" answerable from SQL
  rather than from log archaeology. It also means the table is not a list of
  documents; it is a list of attempts.

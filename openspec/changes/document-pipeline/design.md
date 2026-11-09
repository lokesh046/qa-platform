# Design

## Context

See proposal.md — Why. What exists today: `brd_documents` holds one document
type keyed to a Jira issue key, with no lineage, no revisions and no review
state; `app/llm/` has versioned prompt files, an OpenAI-compatible client and
one validator for the OpenSpec Requirement/Scenario shape; the access gate
order lives in `app/modules/jira/service.py` and every caller delegates to it.

Three constraints shape everything below.

- **The access model keys on a project.** A grant is `(user, project_key)`, and
  until now the project came from the issue key. A chain that starts from a
  brief has no issue key, so the project must come from somewhere else or the
  whole authorisation model has a hole in it.
- **`project_key` on `brd_documents` is a `Computed` column derived from the
  issue key.** It cannot survive an optional issue.
- **Documents are rows, not files** (ADR-024), and regeneration appends.

## Goals / Non-Goals

**Goals**

- One lifecycle implementation that every document type uses, so a fifth stage
  costs a type, a prompt and a validator.
- Lineage that is recoverable: from any document back to the brief, naming the
  exact revision of each source it was built from.
- Staleness that cannot drift out of date, because it is derived rather than
  stored.
- A migration that keeps the CHECK constraints already verified against
  Postgres.

**Non-Goals**

- Concurrent editing. Two people correcting the same document is last-write-
  wins plus a new revision each; no merge, no locking, no conflict detection.
- A diff algorithm. A revision stores its full text; showing a diff is the
  reader's problem.
- Structured storage of document content. The markdown is what a person
  approves and therefore what is stored, per ADR-024.

## Decisions

### A document's project comes from the brief, not from an issue key

Every chain is rooted in a brief, the brief names its project, and every
document in the chain inherits it. `project_key` stops being a generated
column and becomes an ordinary column copied from the brief at creation.

Alternatives considered. *Derive it from an attached Jira issue* — fails when
no issue is attached, which is now the normal case. *Make the project optional
and fall back to "any registered user may read"* — rejected outright: it turns
the access model off for exactly the documents that contain the most
distilled information about a project.

The cost is losing the generated column's guarantee that the key cannot drift
from the issue key. A CHECK constraint that the project key matches the
brief's is not expressible in table-local SQL, so this becomes a foreign key
to the brief plus a service-level copy. That is weaker, and worth stating
plainly.

### One `documents` table with a type, not a table per type

Every type shares the lifecycle, the lineage, the audit shape and the access
rule; they differ only in their content and the validator applied to it. A
table per type means four copies of the state machine and four migrations
every time the lifecycle changes.

Alternative: *a table per type with a shared lifecycle table pointing at them*
— more faithful to the differing content, and it makes every lifecycle query a
union of four. Rejected on the strength of how little actually differs.

### A document is the identity; a revision holds the text

Two different things both look like "a new version":

- the model produced **another attempt** — a new `documents` row, new lineage,
  its own cost;
- a person **corrected** a document — a new `document_revisions` row on the
  same document.

Collapsing them loses the distinction between "we generated this twice" and
"somebody fixed a sentence", which is precisely what the review trail is for.
So `documents` holds identity, type, lineage, state and cost; `document_revisions`
holds text, author, timestamp and note. A generated document starts with
revision 1, authored by the platform.

Alternative: *one table, every row a revision* — simpler to query, but then
cost and lineage are attached to a revision that may be a human edit, and
"which attempt did this come from" stops being answerable.

### Staleness is computed, never stored

A derived document records the **source revision id** it was generated from.
It is stale when that id is not the source's current revision id. No flag, no
job to maintain it, and no way for it to be wrong.

Alternative: *a `stale` boolean updated when a source is edited* — needs a
cascade on every edit, and the cascade is exactly the thing that will one day
miss a row.

### Verification is not exposed as an MCP tool

Generating and reading are MCP tools. **Verifying and rejecting are not.** A
tool is called by a model deciding what to do next, and the one thing the
diagram insists on is that a person verifies each stage. A model that can
verify will verify, because verification is the obstacle between it and the
next stage.

Verification goes through the HTTP API, where the caller is a person acting
deliberately. This is a deliberate reduction in convenience: a QA engineer
cannot say "verify it and generate the FSD" in one sentence to Copilot.

### Validation is a registry keyed by type

`normalise()` — stripping fences and preamble — is shared. The shape check is
per type: BRD and FSD use the existing Requirement/Scenario validator; a step
procedure needs ordered steps; a story breakdown needs stories with tasks and
subtasks. One lookup from type to validator, each returning the same report.

### The phase label is a line in the markdown, parsed by the validator

`**Phase:** 2` directly under a requirement, step or story heading. Readable
by a person in the document they are approving, and machine-checkable by the
validator, which lifts it into counts on the row.

Alternative: *a structured JSON column alongside the markdown* — better for
querying, and it means the approved text and the queried data can disagree.
Rejected for the same reason ADR-024 rejected storing only the structure.

### Module layout: one module owns the aggregate

`app/modules/documents/` owns briefs, documents, revisions, generation and the
state transitions. `app/modules/brd/` is replaced by it.

The scaffold reserved `app/modules/approvals/` for "a generated BRD waits here
until a reviewer approves or rejects it", which is this. It is not used,
because the state machine and the revisions are one aggregate: splitting them
across two modules puts one table's invariants in two services, and the
invariant "an edit returns a document to awaiting verification" would straddle
the boundary. `approvals/` is left for Phase 3, where a *test run* waits for
sign-off — a genuinely separate thing. Its README needs correcting to say so.

## Risks / Trade-offs

**Editing a brief marks the entire chain stale** → Staleness is informational
on read and blocking only on generation, so a stale chain is still fully
readable and a user is told exactly which link to re-verify.

**Four stages multiply cost and rate limit** → One chain is four generations
rather than one, against a free tier that is rate limited per model. Reuse is
the default (regeneration must be asked for), cost is recorded per attempt
including failures, and 429 is already its own error type rather than a
generic failure.

**Markdown as the carrier for phase labels is brittle** → A model that writes
`Phase 2:` instead of `**Phase:** 2` loses the label. Mitigated by the
validator rejecting content it cannot parse rather than silently dropping the
phase, and by the prompt stating the exact form.

**Losing the generated `project_key`** → The project key is now copied rather
than derived, so a bug could write a document under the wrong project, which
is an authorisation bug. Mitigated by a foreign key to the brief and by
deriving the value in exactly one place; not fully mitigated, and it is the
weakest point in this design.

**Last-write-wins on concurrent edits** → Two people correcting the same
document both get a revision, and the later one is current; the earlier is not
lost, but it is not merged either. Acceptable while the reviewer set is small;
revisit if it bites.

**A four-stage chain amplifies a bad prompt** → An error in the BRD propagates
into the FSD, the steps and the stories. The verification gate between stages
is the mitigation, and it only works if people actually read what they verify.

## Migration Plan

1. Create `briefs` and `document_revisions`.
2. Rename `brd_documents` to `documents`; add `type`, `brief_id`,
   `source_document_id`, `source_revision_id`, and the lifecycle columns;
   drop the `Computed` definition from `project_key` and make it an ordinary
   column.
3. Move each existing row's `markdown` into a revision 1 row, then drop the
   column.
4. Backfill `type = 'brd'`. Existing rows have no brief; they are development
   data only, so they are deleted rather than fabricated a brief — stated
   here so the deletion is a decision rather than a surprise.
5. Re-assert the CHECK constraints that already exist, plus the new lifecycle
   ones.

Rollback is the reverse migration. The only data at risk is development data,
and `alembic downgrade` restores the shape rather than the rows.

## Open Questions

- Should a rejected document be regenerable with the rejection note added to
  the prompt as context? It would make rejection useful rather than merely
  final, and it changes no spec: the note is already stored.
- Should a story breakdown carry an estimate or acceptance criteria per story?
  Answerable after seeing what the first real breakdowns look like.
- How long should a chain be kept? No retention policy exists for documents
  (ADR-024 already flags this), and four documents per chain makes it arrive
  sooner.

# Design

## Context

See proposal.md — Why. The architecture changed after the specs were first
written, so this document describes a different system from the one the
repository currently contains.

What exists and is reusable: a pure validator for the Requirement/Scenario
shape, an ADF-to-text parser, a Jira HTTP client using Basic auth, an MCP
server with argument validation and audit discipline, and versioned prompt
files. What exists and is retired: a FastAPI application, five Postgres
tables, Alembic migrations, Redis, a shared service token, and the
`users`/`user_project_access` access model.

Three facts constrain the design.

- **Atlassian Cloud does not reliably support secretless PKCE** for public
  clients. The feature request is open and the only reports of a hidden flag
  are unverified, so a desktop client must carry a client secret.
- **The Postgres schema does not port.** Generated columns, JSONB, partial
  indexes and `ON CONFLICT` are all load-bearing in it.
- **An MCP client can write files.** Both Copilot and Claude Code can. That
  bounds what any file-based control can promise.

## Goals / Non-Goals

**Goals**

- Installing the tool is adding one MCP entry; everything else happens on
  first use.
- One lifecycle implementation that every document type uses.
- Lineage that names the exact source revision, so staleness is derived rather
  than maintained.
- The tool holds no model credential and no Jira credential of its own.

**Non-Goals**

- Multi-user anything. One person, one machine, one store.
- Unattended execution. Nothing runs without a client connected.
- Keeping the retired backend working. It is kept for reference, not
  maintained against the new model.
- Merging concurrent edits. Single user, so the case is rare rather than
  solved.

## Decisions

### The MCP server is the product; the backend is kept but labelled retired

Everything a user touches is the stdio MCP server. The FastAPI application and
its Postgres layer stay in the repository under a `RETIRED.md` that names what
they were for, when they stopped being used, and what would have to be true to
revive them.

The honest risk of "MCP first, backend later" is two divergent data models:
SQLite that people use and Postgres that nobody runs, drifting apart until the
Postgres one is fiction. The mitigation is labelling rather than discipline —
its tests are not run in the default suite, and `RETIRED.md` says the schema
describes a system that no longer exists.

Alternative: *delete it*. Cleaner, and it throws away the only design that can
ever run unattended, which Phase 3 onward will want. Rejected on that basis,
with the cost stated rather than hidden.

### The client writes the documents; a tool checks and stores them

Two operations per stage: one that returns the source content plus the
instructions for the shape required, and one that accepts the drafted text,
checks it and stores it. The model doing the writing is the one already in the
user's conversation.

This is not MCP sampling. Sampling is a server-to-client request, deprecated
in the 2026-07-28 protocol and implemented by roughly two clients in
twenty-four (ADR-021). Here the client generates during its own turn and then
calls an ordinary tool, which every client supports.

What it costs: the prompt is no longer ours, so output varies with the client
and the model behind it. The validator constrains **shape** — a requirement
has scenarios, a scenario has an expected outcome, a phase label is in the
form that can be read — and constrains **quality** not at all. A well-formed
document about the wrong thing passes. Human verification is the only control
on that, which is the argument for the lifecycle rather than an aside to it.

### Jira: browser OAuth with an embedded client secret

One Atlassian OAuth app, registered once. Its client id and secret ship with
the tool. First use opens the consent page against a loopback redirect, the
refresh token goes to the OS keychain, and renewal is silent.

The embedded secret is extractable from any installation. That is accepted:
the secret alone authorises nothing, because the flow still requires a human
to consent in their own browser, and the redirect is loopback so an
authorization code cannot be intercepted remotely. This is what every desktop
OAuth client does, including `gh`.

Alternatives. *Secretless PKCE* — correct, and unavailable on Atlassian Cloud
today. *A hosted broker holding the secret* — removes the embedded secret and
reintroduces a server to run, which is the thing this change exists to remove.
*Per-user API tokens* — no registration and no secret, but it is the manual
paste step this change is meant to delete.

### Authorisation moves to Jira

Per-user OAuth means Jira decides what each person may read. The `users`
table, `user_project_access`, the service account and `X-Actor-Email` all go.

This is a security improvement, not only a simplification: the previous model
had one service account whose visibility was an upper bound on everything, and
a shared token that let any caller assert any actor. Both are gone because
neither exists any more.

What is lost with them: the ability to grant someone access to a project in
this platform that they do not have in Jira, and the ability to answer "who
read what" across a team. Neither is meaningful for a single-user tool.

### A document is the identity; a revision holds the text

Unchanged from the previous design, and the reasoning survives the
architecture change. "The client wrote it again" is a new document with its
own lineage; "a person fixed a sentence" is a new revision on the same
document. Collapsing them loses the distinction the review trail exists for.

### Staleness is computed, never stored

A derived document records the source **revision** it was written from. It is
stale when that is not the source's current revision. No flag, no cascade, and
nothing that can be wrong.

### Verification is a watched review file, and it is a speed bump

The tool writes the document to a review file in its own directory — not in
the repository the editor has open — with a status line at the top. The person
changes the status to verified or rejected, optionally edits the text, and
saves. The tool applies the new text as a revision first, then the state, so
somebody verifies what they actually read.

**This is not a security boundary, and the design should not pretend it is.**
An agent with file-write access could edit that file. Three things make it
reasonable anyway:

- There is no tool that verifies, so an agent must be *asked* to forge an
  approval rather than wander into it while pursuing the next stage. That is
  the failure mode worth preventing: a model that treats verification as an
  obstacle between it and the next stage.
- The file lives outside the workspace, so a repository-scoped agent does not
  encounter it.
- The person who could ask an agent to forge it is the person whose approval
  it is.

Alternatives. *A CLI command* — genuinely unforgeable by a chat agent, and it
makes you leave the conversation to approve something you just read in it.
*A code printed to the terminal that the verify tool requires* — keeps the
flow in chat, and is one copy-paste from being handed to the model, which
makes it theatre. The file was chosen with the weakness accepted.

### A fresh SQLite schema, not a port

Tables for briefs, documents, revisions and local records. No generated
columns: the project key is written by the application and carried from the
brief. No JSONB: the few structured fields are columns. No partial indexes.
`ON CONFLICT` is not needed, because one process writes.

The schema is created and upgraded on startup, with a version number in the
file. A store written by a newer version is refused rather than written to —
an old binary writing an old shape over new data is the one unrecoverable
mistake available here.

ADR-018 forbade SQLite for *testing the Postgres schema*, because a suite
passing against SQLite would prove the code works against a database the
project does not use. A separate, deliberately simpler schema for a different
product is not that, and this design note exists so the two are not confused.

### MCP tools, and what is deliberately absent

Present: submit a brief, read a brief, start a stage, store a drafted
document, read a document, read a document's history, check authentication
status, sign out.

Absent: anything that verifies, rejects or deletes. Deletion does not exist at
all; verification exists only through the review file.

## Risks / Trade-offs

**A model could be asked to forge an approval** → No verify tool exists, the
review file is outside the workspace, and the design records this as accepted
rather than solved. On a single-user tool, the person who could ask is the
person approving.

**Output consistency drops** → The client's prompt replaces ours. The
validator rejects wrong shapes and cannot judge content; human verification is
the control, and the chain refuses to advance without it.

**The embedded client secret is extractable** → It authorises nothing without
a user consenting in their own browser, and the loopback redirect prevents
remote interception of a code.

**Two data models drift** → `RETIRED.md`, and the retired tests are out of the
default suite, so nobody mistakes a passing run for the backend still working.

**The store is a single file on one machine** → No backup, no sync, no
recovery if the disk dies. It is portable by copying, and that is the whole of
the story. Worth naming before somebody has eight weeks of documents in it.

**Nothing runs unattended** → Phases 3 to 5 assumed a server and a job queue.
Playwright can run locally on demand from a tool, so the use case survives;
scheduled or queued work does not.

**A four-stage chain amplifies a bad first document** → An error in the BRD
propagates into everything after it. The verification gate between stages is
the mitigation, and it works only if people read what they verify.

## Migration Plan

There is no data migration: the Postgres store holds development data only,
and the new store starts empty.

1. Add `RETIRED.md` and exclude the retired tests from the default suite, so
   the first commit of this change states what is no longer live.
2. Build the local store, authentication and the tools alongside the existing
   code; nothing is deleted until the MCP path works end to end.
3. Remove the HTTP routers and the backend client from the MCP server once no
   tool calls them.

Rollback is `git revert`. Nothing in production depends on either path.

## Open Questions

- Should a rejected document be writable again with the rejection note
  supplied as context? It would make rejection useful rather than merely
  final, and it changes no spec: the note is already stored.
- Should the review file be offered in the editor automatically, or only its
  path reported? Depends on what is tolerable inside a chat flow, and is
  answerable after using it.
- How long should a chain be kept, and should the store ever be pruned? Not
  urgent while it is one file one person owns.

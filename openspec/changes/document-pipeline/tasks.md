# Tasks

## 1. Data model and migration

- [ ] 1.1 Add `briefs` (text, project key, submitter, submitted at) and
      `brief_jira_issues`, with CHECK constraints for non-empty text, the
      length bound, and a lowercase submitter email; verify by attempting an
      empty brief, an oversized brief and a mixed-case email in `psql` and
      confirming each is refused by name
- [ ] 1.2 Rename `brd_documents` to `documents`; add `type`, `brief_id`,
      `source_document_id`, `source_revision_id`, `verification_state`,
      `verified_by`, `verified_at` and `rejection_note`; convert
      `project_key` from a `Computed` column to an ordinary column with a
      foreign key path to the brief; verify `alembic upgrade head` then
      `alembic downgrade -1` then `upgrade head` again leaves `\d documents`
      identical
- [ ] 1.3 Add `document_revisions` (document, revision number, markdown,
      author email, author kind, note, created at) with a unique constraint on
      (document, revision number); verify by inserting two revisions with the
      same number and confirming the database refuses the second
- [ ] 1.4 Move `markdown` out of `documents` into revision 1 and drop the
      column; verify no row in `documents` has a markdown column and every
      pre-existing document has exactly one revision
- [ ] 1.5 Re-assert every CHECK constraint from `brd_documents` and add the
      lifecycle ones (a verified document has a verifier and a time; a
      rejected one has a note); verify by attempting each violation in `psql`
      and confirming all are refused
- [ ] 1.6 Write ADR-025 recording that `project_key` is no longer a generated
      column and what that costs, since it weakens an authorisation
      invariant; verify the ADR names the alternative considered and the
      residual risk

## 2. Brief intake

- [ ] 2.1 Add `app/modules/documents/` with `schemas.py`, `repository.py`,
      `service.py`, `router.py`, replacing `app/modules/brd/`; verify
      `uv run ruff check .` passes and no module imports `app.modules.brd`
- [ ] 2.2 Implement brief submission with the access gates (registered,
      active, holds a grant for the named project) running before anything is
      stored; verify a unit test asserts no row is written when the gate
      refuses
- [ ] 2.3 Implement attaching Jira issues as context, refusing the whole
      submission if any referenced issue is inaccessible or missing; verify a
      unit test submits two issues where one is inaccessible and asserts
      nothing was stored
- [ ] 2.4 Implement reading a brief, refused identically for "no access" and
      "does not exist"; verify a unit test asserts the two responses are
      byte-identical
- [ ] 2.5 Add `POST /api/v1/briefs` and `GET /api/v1/briefs/{id}`, and
      document both in `docs/api.md` including the audit rows each writes;
      verify the documented `curl` commands run as written against the local
      stack

## 3. Generation, generalised

- [ ] 3.1 Generalise `app/llm/prompt.py` to build a prompt for any stage from
      a source document plus optional Jira context, keeping the delimiting of
      untrusted content; verify existing prompt tests still pass and a new
      test asserts source text is inside the data delimiters
- [ ] 3.2 Add prompt files for `fsd_v1`, `step_procedure_v1` and
      `story_breakdown_v1`, each stating the exact `**Phase:** <n>` form;
      verify a test asserts every stage has a prompt pair and a version
- [ ] 3.3 Add a validator registry keyed by document type, reusing
      `normalise()`; verify unit tests cover a valid and an invalid example
      for each of the four types
- [ ] 3.4 Implement step-procedure validation (ordered steps, each with an
      action and an expected result); verify tests reject an unordered list
      and a step with no expected result
- [ ] 3.5 Implement story-breakdown validation (stories, each with tasks and
      optional subtasks); verify tests reject a story with no tasks and a
      subtask with no parent task
- [ ] 3.6 Parse `**Phase:** <n>` in every validator and carry it onto the
      stored counts; verify a test generates a chain fixture where one
      requirement is Phase 2 and asserts the derived steps and stories are
      Phase 2
- [ ] 3.7 Implement generation from a source document, refusing an
      unsupported stage pairing by name; verify tests cover each supported
      pairing and at least two unsupported ones

## 4. Lifecycle

- [ ] 4.1 Implement verification and rejection with a note, refusing callers
      without a grant; verify unit tests cover verify, reject, and a refusal
      leaving the state unchanged
- [ ] 4.2 Implement correction as a new revision, never overwriting, and
      returning the document to awaiting verification; verify a test corrects
      a verified document twice and asserts both earlier revisions are still
      readable and the state is awaiting verification
- [ ] 4.3 Implement staleness as a computed comparison of
      `source_revision_id` against the source's current revision; verify a
      test edits a source and asserts the derived document reads as stale
      without any write having touched it
- [ ] 4.4 Refuse generation from a source that is unverified, rejected or
      stale, each with its own message; verify three unit tests, one per case
- [ ] 4.5 Implement reading a document's history (revisions and state
      changes, in order, with actors); verify a test asserts ordering and
      that a caller without access is refused
- [ ] 4.6 Confirm no delete path exists for briefs, documents or revisions;
      verify a test enumerates the router's operations and asserts none is a
      delete
- [ ] 4.7 Audit every lifecycle action including refusals; verify a test
      asserts an audit event for each of verify, reject, correct and history
      read, in both the allowed and refused cases
- [ ] 4.8 Correct `app/modules/approvals/README.md` to say it is for Phase 3
      test-run sign-off, not document verification; verify the README no
      longer claims to own BRD approval

## 5. Endpoints and the chain end to end

- [ ] 5.1 Add the document endpoints (generate, read, verify, reject,
      correct, history) and remove `POST /api/v1/brd` and
      `GET /api/v1/brd/{issue_key}`; verify the OpenAPI schema lists the new
      paths and neither old one
- [ ] 5.2 Document every new endpoint in `docs/api.md` with its required
      role, its audit rows, and the 404-before-503 ordering; verify each
      documented example runs against the local stack
- [ ] 5.3 Run the full chain against a local stand-in provider, as Step 12
      did: brief, BRD, verify, FSD, verify, step procedure, verify, story
      breakdown; verify every stage produces a document, each refusal before
      verification behaves, and the phase label survives to the stories
- [ ] 5.4 Confirm Jira is untouched by the whole run; verify by comparing the
      issue list and each referenced issue's `updated` timestamp before and
      after

## 6. MCP tools

- [ ] 6.1 Add `submit_brief`, `generate_document` and `read_document` tools,
      each validating arguments, writing an audit record and forwarding, with
      no business logic in the gatekeeper; verify tests drive them through
      the SDK's in-process client
- [ ] 6.2 Assert that no tool can verify or reject a document; verify a test
      enumerates the exposed tools and asserts none performs verification
- [ ] 6.3 Document every tool in `docs/mcp-tools.md` with its arguments,
      validation rules, backend endpoint and whether it reads or writes;
      verify the documented argument shapes match the tools' schemas
- [ ] 6.4 Assert no internal failure text reaches the model; verify a test
      makes the backend raise with a connection string in the message and
      asserts it is absent from what the client receives

## 7. Integration verification

- [ ] 7.1 Build the integration fixture the project still lacks — a separate
      test database, one `alembic upgrade head`, a transaction rolled back
      per test; verify two tests sharing a table do not see each other's rows
- [ ] 7.2 Cover what unit tests cannot: the CHECK constraints, the revision
      uniqueness constraint, and that reading a server-generated column after
      an update does not raise; verify the suite fails if the
      `_reload_server_values` call is removed
- [ ] 7.3 Write the step log and the CHANGELOG entry for this change, and
      update the README's project structure and environment variables;
      verify `docs/progress/` has an entry naming every ADR this change added

## Workflow follow-up

- Archive this change once implemented: `openspec archive document-pipeline`.
- Stage 5 of the diagram (Jira context to Python script) is a separate change.
- Writing stories into Jira is a separate change, behind an approval gate.

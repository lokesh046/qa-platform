# Tasks

## 1. Mark what is retired

- [ ] 1.1 Add `RETIRED.md` naming the FastAPI app, the Postgres models and
      migrations, Redis, the compose product path, the users and grants
      tables, the service token and `mcp_server/backend_client.py`, with when
      and why they stopped being used; verify it names every path listed in
      proposal.md under "What is retired"
- [ ] 1.2 Exclude the retired modules' tests from the default pytest run and
      keep them runnable by an explicit marker; verify `uv run pytest` passes
      without them and `uv run pytest -m retired` still runs them
- [ ] 1.3 Update `README.md` so the first thing it describes is installing the
      MCP server, with the backend presented as retired; verify a reader
      following only the README reaches a working tool without starting Docker

## 2. Local store

- [ ] 2.1 Design and create the SQLite schema for briefs, documents,
      revisions and local records, with a schema version in the file; verify
      a test creates a store from scratch and reads back a brief, a document
      and two revisions
- [ ] 2.2 Implement create-on-first-use and upgrade-on-start; verify a test
      opens a store written by the previous schema version and finds its rows
      intact afterwards
- [ ] 2.3 Refuse a store written by a newer version rather than writing to it;
      verify a test sets a higher version in the file and asserts the tool
      stops with a message naming the file
- [ ] 2.4 Report an unreadable store rather than silently starting a new one;
      verify a test corrupts the file and asserts the error names the path and
      no empty store replaces it
- [ ] 2.5 Confirm no delete path exists for briefs, documents or revisions;
      verify a test enumerates the store's operations and asserts none deletes
- [ ] 2.6 Write ADR-025 recording the local SQLite store, including why
      ADR-018's "no SQLite" rule does not apply; verify the ADR names the
      alternatives and the single-file backup risk

## 3. Jira authentication

- [ ] 3.1 Implement the browser OAuth flow against a loopback redirect, with
      the authorization code exchanged for tokens; verify an integration test
      drives it against a stubbed Atlassian endpoint and ends with a token
      stored
- [ ] 3.2 Store the refresh token in the OS keychain; verify a test asserts no
      file in the working directory or the tool's directory contains the token
      after sign-in
- [ ] 3.3 Implement silent renewal, and re-consent when renewal fails; verify
      tests cover an expired access token renewing without a prompt, and an
      invalid refresh token asking the user to sign in again
- [ ] 3.4 Implement accessible-site discovery, asking once when there is more
      than one and remembering the choice; verify tests cover one site and
      three
- [ ] 3.5 Implement sign-out and an authentication status report that exposes
      no token; verify a test asserts the status output contains no part of
      any stored value
- [ ] 3.6 Move the Jira client from HTTP Basic to the OAuth bearer token and
      the Atlassian API gateway host; verify the existing Jira client tests
      pass against the new auth with their fixtures updated
- [ ] 3.7 Write ADR-026 recording per-user OAuth, the embedded client secret
      and why secretless PKCE is unavailable; verify the ADR cites the open
      Atlassian request and states what the secret does and does not authorise
- [ ] 3.8 Document registering the Atlassian OAuth app in `README.md`,
      including the exact callback URL and scopes; verify somebody following
      it registers an app the tool can use

## 4. Brief intake

- [ ] 4.1 Implement brief submission with bounds and an immutable text;
      verify tests cover empty, whitespace-only, oversized and valid
- [ ] 4.2 Implement attaching Jira issues read as the signed-in user,
      refusing the whole submission if any is unreadable; verify a test with
      two issues where one is forbidden asserts nothing was stored
- [ ] 4.3 Implement submitting with no Jira issues while signed out; verify a
      test asserts it succeeds without triggering authentication
- [ ] 4.4 Implement reading a brief; verify tests cover a stored brief and an
      unknown identifier

## 5. Stages and shape checking

- [ ] 5.1 Implement starting a stage: return source content plus instructions,
      refusing unsupported pairings by name; verify tests cover all four
      supported stages and two unsupported ones
- [ ] 5.2 Refuse starting a stage whose source is unverified, rejected or
      stale, each with its own message; verify three tests, one per case
- [ ] 5.3 Convert the prompt files into the instructions a stage returns, one
      per stage, each stating the exact phase-label form; verify a test
      asserts every stage has instructions and a version
- [ ] 5.4 Implement storing a drafted document, checking shape before storing
      and reporting every problem at once; verify a test submits a draft with
      two distinct faults and asserts both are reported and nothing is stored
- [ ] 5.5 Add shape checks for step procedure and story breakdown alongside
      the existing Requirement/Scenario one, in a registry keyed by stage;
      verify tests cover a valid and an invalid example for each of the four
- [ ] 5.6 Parse and require the phase label, refusing an unreadable one rather
      than dropping it; verify a test marks a requirement Phase 2 and asserts
      derived steps and stories carry it, and a second test asserts a
      malformed label is refused
- [ ] 5.7 Record lineage on every stored document, including the source
      revision; verify a test builds a full chain and asserts each document
      names its source and the revision current at the time
- [ ] 5.8 Record the client-reported model as a claim; verify tests cover a
      client that names one and a client that does not

## 6. Lifecycle

- [ ] 6.1 Implement the review file: write the document with a status line,
      watch it, apply an edited text as a revision before applying the state;
      verify a test edits both text and status and asserts the revision is
      stored first and the state applies to it
- [ ] 6.2 Handle an unreadable status value without changing state; verify a
      test writes nonsense and asserts the document is untouched and the file
      reports the accepted values
- [ ] 6.3 Implement correction returning a document to awaiting verification;
      verify a test corrects a verified document and asserts the state and
      that no stage can be started from it
- [ ] 6.4 Implement staleness as a comparison against the source's current
      revision; verify a test corrects a source and asserts the derived
      document reads stale with no write having touched it
- [ ] 6.5 Implement reading a document's history in order; verify a test
      asserts revisions and state changes interleave correctly by time
- [ ] 6.6 Keep local records of verification, rejection and correction; verify
      a test asserts a record exists for each, independent of the document row
- [ ] 6.7 Correct `app/modules/approvals/README.md` to say it is for Phase 3
      test-run sign-off; verify it no longer claims to own document approval

## 7. The tools

- [ ] 7.1 Expose the tools: submit brief, read brief, start stage, store
      document, read document, read history, auth status, sign out; verify a
      test drives each through the SDK's in-process client
- [ ] 7.2 Assert no tool verifies, rejects or deletes; verify a test
      enumerates the offered tools and asserts none of the three exists
- [ ] 7.3 Keep source content and drafted text out of every log line; verify a
      test fails a stage with distinctive content and asserts it appears in no
      log entry
- [ ] 7.4 Keep internal failure text away from the model; verify a test raises
      with a connection string in the message and asserts it is absent from
      what the client receives
- [ ] 7.5 Document every tool in `docs/mcp-tools.md` with arguments,
      validation rules and whether it reads or writes; verify the documented
      shapes match the tools' schemas

## 8. End to end

- [ ] 8.1 Run the full chain in Claude Code against a real Jira account:
      brief, BRD, verify, FSD, verify, step procedure, verify, story
      breakdown; verify every stage produces a document and each refusal
      before verification behaves
- [ ] 8.2 Confirm Jira is untouched by the whole run; verify by comparing each
      referenced issue's `updated` timestamp before and after
- [ ] 8.3 Confirm installation is one MCP entry on a machine that has never
      run the tool; verify from a clean home directory that first use signs in
      and creates the store with no other step
- [ ] 8.4 Write the step log and CHANGELOG entry, and update the README's
      project structure and configuration; verify `docs/progress/` has an
      entry naming every ADR this change added

## Workflow follow-up

- Archive this change once implemented: `openspec archive document-pipeline`.
- Stage 5 of the use case (Jira context to Python script) is a separate change.
- Writing stories into Jira is a separate change.
- Reviving the retired backend for unattended work is a separate change.

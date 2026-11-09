# Proposal

## Why

The platform can turn one Jira issue into one BRD and then stops. The work it
is meant to support is a chain: a free-text brief becomes a BRD, the BRD
becomes an FSD, the FSD becomes a step procedure, and the step procedure
becomes a set of proposed Jira stories broken into tasks and subtasks. Each
link is read by a person, corrected, and kept as a version before the next
link is drawn from it.

Two things are wrong today. Nothing supports that chain: a generated document
cannot be reviewed, cannot be corrected, has no revision anybody can see, and
cannot feed the next stage. And the thing a QA engineer would have to install
is a platform — Postgres, Redis, a FastAPI service, a seeded user table, a
model provider credential — to get a document written.

The shape of the work matters more than any single stage. **"Generate → human
verifies → changes → version" repeats identically at every link.** Built once,
with the document type as a parameter, each new stage costs a type, a prompt
and a validator.

And the thing doing the generating is already in the room. The QA engineer is
talking to Copilot, which has a model, has been given the ticket, and can
write the document itself. The platform's job is not to own a model. It is to
supply the context, say what shape the answer must take, refuse an answer that
is the wrong shape, and keep every version.

## What Changes

- **BREAKING: the product becomes a single local MCP server.** Somebody adds
  one entry to their MCP configuration and has the whole chain. No service to
  deploy, no database to run, no tokens to paste.
- **BREAKING: the HTTP API, Postgres, Redis and the shared service token leave
  the product path.** `POST /api/v1/brd` and `GET /api/v1/brd/{issue_key}` are
  removed rather than replaced. The backend code remains in the repository,
  **explicitly marked retired**, for the later phase that needs unattended
  execution.
- **BREAKING: the platform stops owning a model.** The MCP client writes each
  document; a tool validates the result and stores it. No `LLM_API_KEY`, no
  provider cost, no provider outage. This is an ordinary tool call, not MCP
  sampling, which ADR-021 rejected as deprecated and supported by roughly two
  clients in twenty-four.
- **BREAKING: the platform stops owning authorisation.** Each person signs in
  to Jira in their own browser, and Jira enforces their own permissions. The
  `users` and `user_project_access` tables, the service account and the
  `X-Actor-Email` header are removed. ADR-002 called per-user OAuth "the right
  long-term answer" and deferred it; this is it.
- **Jira authentication is a browser login.** First use opens a consent page;
  the refresh token lands in the operating system keychain and renews itself.
- **State is a local SQLite database**, holding briefs, documents, revisions
  and verification state for one person. Designed fresh — the Postgres schema
  uses generated columns, JSONB, partial indexes and `ON CONFLICT`, none of
  which port.
- **A brief can be submitted** as free text and becomes the root of a chain,
  optionally naming Jira issues as supporting context.
- **A document can be generated from the document before it**, so the chain is
  `brief → BRD → FSD → step procedure → story breakdown`. Each stage refuses
  until the document it derives from has been verified by a person.
- **A person verifies, rejects or corrects a document**, and every correction
  becomes a revision recording who, when and what. Nothing is overwritten.
- **A delivery phase label travels down the chain.** Phase 1 and Phase 2
  describe the product being specified, so a requirement marked Phase 2 in the
  FSD produces Phase 2 steps and Phase 2 stories.
- **A story breakdown is a document, not a Jira write.** Nothing is created in
  Jira.

## Capabilities

### New Capabilities

- `jira-authentication`: signing a person in to Jira from a desktop tool,
  keeping the token where the operating system keeps secrets, renewing it
  silently, and asking again when renewal fails.
- `local-store`: the single-user store that holds briefs, documents, revisions
  and verification state, and what is guaranteed about where it lives and what
  it keeps.
- `brief-intake`: accepting and storing the free-text brief that starts a
  chain, bounding it, and attaching optional Jira issues as context. The only
  capability whose content comes from a person rather than a model.
- `document-generation`: supplying the client with the context and the shape a
  stage requires, checking what comes back, carrying the delivery phase label
  forward, and recording the lineage of every document.
- `document-lifecycle`: the states a document moves through, how a person
  verifies or rejects one, how a correction becomes a revision, and what "the
  current version" means.

### Modified Capabilities

None. The project has no specs yet; these are the first.

## Impact

**What a user installs**

One MCP server entry. On first use it opens a browser for Jira consent and
creates its database. Nothing else.

**What survives in the codebase**

- `app/llm/openspec_format.py` — the validator. Pure, and the reason a wrongly
  shaped document never reaches the store.
- `app/modules/jira/adf.py` — Atlassian Document Format to text.
- `app/modules/jira/client.py` — the Jira gateway, moving from HTTP Basic to
  an OAuth bearer token.
- `mcp_server/` — its structure, argument validation and error discipline.
- `app/llm/prompts/` — repurposed as the instructions a tool returns to the
  client, instead of prompts this platform sends to a provider.
- The tests for all of the above, and every ADR.

**What is retired, and must be labelled as retired**

`app/main.py` and the FastAPI application, `app/modules/*/router.py`, the
Postgres models and migrations, Redis, the product path through
`docker-compose.yml`, the `users` and `user_project_access` tables,
`audit_logs` as a shared trail, `app/core/security.py`'s service token, and
`mcp_server/backend_client.py`.

**What is lost**

- **Unattended execution.** With no server and no queue, nothing runs unless a
  person has an editor open. Phases 3 to 5 as originally conceived assumed
  both.
- **A shared audit trail.** A single-user local tool keeps a local record.
  That is weaker, and on a tool nobody else can reach, much less necessary.
- **Prompt control.** The client writes the document, so output varies with
  the client and the model behind it. The validator constrains shape, not
  quality.

**Out of scope**

- Generating the Python test script (stage 5 of the use case).
- Creating anything in Jira.
- Remote or multi-user access, including the OAuth server work parked in
  Step 10.
- Making the retired backend work against the new model. It is kept, not
  maintained.

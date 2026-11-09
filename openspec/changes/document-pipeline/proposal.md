# Proposal

## Why

The platform can turn one Jira issue into one BRD and then stops. The work it
is meant to support is a chain: a free-text brief becomes a BRD, the BRD
becomes an FSD, the FSD becomes a step procedure, and the step procedure
becomes a set of Jira stories broken into tasks and subtasks. Each link is
reviewed by a person, corrected, and kept as a version before the next link is
drawn from it.

Nothing in the platform supports that today. A generated document cannot be
reviewed, cannot be corrected, has no revision anybody can see, and cannot be
used as the input to the next document. The one generator that exists takes a
Jira issue key, which is not where the chain starts.

The shape of the work matters more than any single stage. **"Generate → human
verifies → changes → version" repeats identically at every link.** Built once,
with the document type as a parameter, each new stage costs a type and a
prompt. Built per stage, the platform acquires four nearly-identical review
flows that drift apart, and four ways to lose somebody's correction.

## What Changes

- **BREAKING: `POST /api/v1/brd` changes its input.** It takes a brief today's
  callers do not have. A BRD is generated from a submitted brief, optionally
  with Jira issues as additional context, rather than from an issue key. The
  existing endpoint and its request schema are replaced.
- **BREAKING: `brd_documents` becomes a general document table.** A document
  gains a type (`brd`, `fsd`, `step_procedure`, `story_breakdown`), a link to
  the document it was derived from, and a revision chain. The current table
  holds one type and no lineage.
- **A brief can be submitted** as free text and becomes the root of a chain.
  Zero or more Jira issues may be attached as supporting context.
- **A document can be generated from another document**, so the chain is
  `brief → BRD → FSD → step procedure → story breakdown`. Each stage refuses
  to run until the document it derives from has been verified by a person.
- **A document can be verified, rejected or edited by a person**, and every
  edit creates a new revision recording who changed it, when, and what changed.
  Nothing is overwritten and nothing is deleted.
- **A delivery phase label travels down the chain.** Phase 1 and Phase 2
  describe the product being specified, not this platform's roadmap, so a
  requirement marked Phase 2 in the FSD produces Phase 2 steps and Phase 2
  stories.
- **A story breakdown is a document, not a Jira write.** It proposes stories
  with their tasks and subtasks for a person to act on. The platform creates
  nothing in Jira.
- **MCP tools** for the stages a QA engineer drives from Copilot Chat or Claude
  Code, so the chain is usable without touching the HTTP API directly.

## Capabilities

### New Capabilities

- `brief-intake`: accepting and storing the free-text brief that starts a
  chain, bounding it, and attaching optional Jira issues as context. The only
  capability whose content comes from a person rather than a model.
- `document-generation`: producing a document of a given type from the document
  before it in the chain, validating the result against the shape that type
  requires, carrying the delivery phase label forward, and recording what the
  attempt cost.
- `document-lifecycle`: the states a document moves through, who may verify or
  reject one, how an edit becomes a revision, and what "the current version of
  this document" means. The wrapper every stage shares.

### Modified Capabilities

None. The project has no specs yet; these are the first.

## Impact

**Affected code**

- `app/modules/brd/` — generalised from one document type to many. Its table,
  schemas, repository, service and endpoints all change shape.
- `app/models/brd_document.py` — becomes the general document model, plus new
  models for briefs and revisions. Needs a migration that preserves the
  existing CHECK constraints and adds the lifecycle ones.
- `app/modules/approvals/` — currently an empty placeholder. Becomes the
  verification half of `document-lifecycle`.
- `app/llm/prompts/` — one prompt pair per document type, each versioned, each
  recorded on the documents it produces.
- `mcp_server/` — new tools. The gatekeeper rule holds: validate, audit,
  forward, no business logic.

**Affected contracts**

- `POST /api/v1/brd` is replaced. No external consumer exists yet, so the break
  costs nothing now and would cost a migration later.
- `GET /api/v1/brd/{issue_key}` is replaced: a document is identified by its
  own id and its chain, not by an issue key, now that an issue is optional.

**Preserved**

- The access gate order — registered, active, holds a project grant, then
  cache or Jira — is a security property and does not change. Every new
  endpoint runs it before anything is generated or read.
- ADR-024's decision that documents are database rows rather than files, and
  that regeneration appends rather than overwrites.
- Jira stays read-only.

**Out of scope**

- **Generating the Python test script** (stage 5 of the diagram). The least
  defined stage, and it depends on the story breakdown existing first.
- **Creating issues in Jira.** Stories are proposed as a document. Writing them
  is a separate change, behind an approval gate, once the quality of generated
  stories is known.
- **Remote access and OAuth.** Started and parked in Step 10; unrelated to this.
- **Git or pull-request review of documents.** Versioning is in the platform.
- **Editing a document through a diff or merge UI.** An edit replaces the text
  and creates a revision; reconciling two concurrent edits is not solved here.

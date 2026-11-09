# ADR-019: OpenSpec is a development tool, and separately a document format

Date: 2026-11-09
Status: Accepted

## Context

The brief for Phase 2 says "OpenSpec BRD generation". Before building it we
checked what OpenSpec is. The answer changed the design, so it is recorded here.

`@fission-ai/openspec` 1.16.1 (MIT, published 2026-11-07) is a spec-driven
development framework for AI coding agents. A developer types `/opsx:propose
add-dark-mode` in Claude Code or Copilot Chat; the agent writes the proposal,
specs, design and tasks; the CLI scaffolds, validates and archives them.

Three facts matter:

1. **There is no `openspec generate`.** Its commands are `change`, `spec`,
   `show`, `validate`, `workset`, `config`, `doctor`, `store`. Its dependencies
   are `commander`, `@inquirer/prompts`, `chalk`, `ora`, `yaml`, `zod` — a
   terminal CLI, with no LLM client anywhere. It cannot turn a Jira ticket into
   requirements. A model does that; the CLI manages the files around it.
2. **It is interactive and needs Node.js >= 22.12.0.** This backend is Python
   3.12.
3. **Telemetry is on by default.** It reports command names and version unless
   `telemetry.enabled` is false or `OPENSPEC_TELEMETRY=0` is set.

Separately, Step 1 had scaffolded `specs/openspec/` as "the OpenSpec working
directory" — which is exactly what `openspec init` creates, at `openspec/`. Two
directories for one purpose, with the same name in the path.

## Decision

OpenSpec plays **two unrelated roles** in this repository, and they are kept
apart deliberately.

1. **A development workflow, for us.** `openspec init --tools
   claude,github-copilot` created `openspec/` at the repository root, plus
   24 instruction files under `.claude/` and `.github/`. Phases 3 to 5 are
   planned as OpenSpec changes before they are coded. Nothing at runtime reads
   or writes `openspec/`.
2. **A document format, for the product.** The BRDs the `brd` module generates
   use OpenSpec's markdown conventions — `## ADDED Requirements`,
   `### Requirement:`, `#### Scenario:` with **WHEN** / **THEN** — because
   named scenarios in a fixed shape are good input for DSPy test generation in
   Phase 3. They are product output, not part of our workflow.

And the boundary:

3. **The `openspec` CLI is never invoked from an HTTP request.** It is a
   developer and CI dependency. It is not installed in the Docker image, so
   the API container stays single-runtime. `openspec validate` runs in CI and
   from the worker, which is where a structural check on generated output
   belongs.
4. **Telemetry is off.** `openspec config set telemetry.enabled false` has been
   run, and CI sets `OPENSPEC_TELEMETRY=0`. An internal platform does not
   report its activity to a third party by inheritance.
5. **`specs/openspec/` is deleted.** `openspec/` is the only OpenSpec working
   directory. Where generated BRDs are stored is a separate decision, made by
   the step that writes the first one.

## Alternatives

- **Node 22 in the API image, shelling out to the CLI per request.** The most
  literal reading of "the backend invokes OpenSpec", and the option originally
  chosen. Rejected once the facts above were known: it adds a second language
  runtime and its patch stream to a container that needs neither, couples a web
  request to a subprocess built for a terminal with interactive prompts, and
  sends telemetry from a server. It also would not generate anything — the
  model does that either way — so the cost buys only `openspec validate`, which
  is better run in CI.
- **OpenSpec's format with our own Pydantic validator, and no CLI at all.**
  Attractive: pure Python, testable by the existing suite. Rejected because a
  real validator exists for free, and a hand-rolled parser of someone else's
  format drifts silently the moment they change it.
- **Our own BRD template, ignoring OpenSpec.** Rejected: the WHEN/THEN scenario
  shape is the part Phase 3 benefits from, and inventing a different structure
  with the same purpose adds no value.
- **Keeping `specs/openspec/` as the output directory.** Rejected on naming
  alone — two paths containing `openspec` with different meanings is a trap for
  the next person — and because it means the API writing into its own source
  tree, which does not survive a container restart.

## Consequences

- Node.js is required to **develop** this project and not to **run** it. The
  README's Setup section says so; the Dockerfile is unchanged.
- If OpenSpec changes its markdown conventions, our generated BRDs drift from
  the format they claim to use. `openspec validate` in CI is what would catch
  it. There is still no CI, which makes this the second concrete reason to add
  it (ADR-018 was the first).
- The word "spec" now means three things here: an OpenSpec change proposal, a
  generated BRD, and a Pydantic schema. `app/modules/brd/README.md` names the
  distinction, because nothing else will.
- Telemetry opt-out is **global developer config, not a repository setting**. A
  new developer who runs `openspec init` elsewhere has it on again. The README
  records the command; nothing enforces it.
- The 24 generated instruction files are committed on purpose. They are the
  shared workflow, they contain no secrets, and leaving them untracked would
  mean every developer getting a slightly different version from whichever CLI
  release they happened to install.
- `openspec update` must be run after a CLI upgrade to refresh those files. Not
  automated, and it will be forgotten at least once.

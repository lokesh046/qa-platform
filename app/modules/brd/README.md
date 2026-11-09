# Module: brd (placeholder)

**Status:** empty. Filled in **Phase 2**.

## Purpose
Turns a Jira issue into a Business Requirements Document. Reads the cached
issue from `jira_issues`, asks the model in `app/llm/` to write requirements in
OpenSpec's markdown format, and records the result so later phases can generate
tests from it.

## Expected contents
`router.py`, `service.py`, `schemas.py`, `repository.py` — the same module
pattern as the Phase 1 modules.

## Do not add here
Prompt text (lives in `app/llm/prompts/`) or workflow orchestration
(lives in `app/workflows/`).

## Note on OpenSpec
Two different things share that name in this repository, and confusing them
wastes an afternoon:

- `openspec/` at the repository root is **our own** development workflow, created
  by `openspec init`. It holds specs and change proposals for building this
  platform. Nothing at runtime reads or writes it.
- The BRDs this module generates are **product output**, one per Jira issue.
  They use OpenSpec's markdown format because it is good structured input for
  DSPy in Phase 3, but they are not part of our development workflow and do not
  live in `openspec/`.

The `openspec` CLI is never invoked from inside an HTTP request. See
[ADR-019](../../../docs/decisions/ADR-019-openspec-role-and-boundaries.md).
Where generated BRDs are stored is decided in Step 11, not here.

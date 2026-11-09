# Module: brd (placeholder)

**Status:** empty. Filled in **Phase 2**.

## Purpose
Turns a Jira issue into a Business Requirements Document using OpenSpec.
Reads the cached issue, produces a structured spec under `specs/openspec/`,
and stores a reference to it so later phases can generate tests from it.

## Expected contents
`router.py`, `service.py`, `schemas.py`, `repository.py` — the same module
pattern as the Phase 1 modules.

## Do not add here
Prompt text (lives in `app/llm/prompts/`) or workflow orchestration
(lives in `app/workflows/`).

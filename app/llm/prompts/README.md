# app/llm/prompts (placeholder)

**Status:** empty. Filled in **Phase 2**.

## Purpose
Prompt templates as plain text or Jinja files, one file per task
(for example `brd_from_issue.md`, `testcases_from_brd.md`).

## Rules
- No credentials or customer data in a template.
- A template is versioned by filename or a header field, so a stored artifact
  can say which prompt produced it.

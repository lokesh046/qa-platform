# app/llm (placeholder)

**Status:** empty. Filled in **Phase 2**.

## Purpose
Everything LLM-specific that is not business logic: model clients, retry and
token-budget helpers, and the prompt templates in `prompts/`.

Keeping prompts in files rather than in Python strings means they can be
reviewed in a diff and changed without touching module code.

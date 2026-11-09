# workers/sandbox (placeholder)

**Status:** empty. Filled in **Phase 4**.

## Purpose
The sandboxed Playwright runner: its own container image, no database
credentials, no network access beyond the target application, a wall-clock
timeout, and a read-only filesystem apart from a scratch directory.

## Why it is separate
It executes LLM-generated code. Treat that code as untrusted. The isolation
here is the control that makes generated automation safe to run.

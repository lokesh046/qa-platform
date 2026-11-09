# Module: execution (placeholder)

**Status:** empty. Filled in **Phase 4**.

## Purpose
Schedules and tracks Playwright runs. Hands work to the sandboxed workers in
`workers/sandbox/`, tracks run state, and collects results and artifacts
(screenshots, traces, video) from object storage.

## Expected contents
`router.py`, `service.py`, `schemas.py`, `repository.py`.

## Security note for whoever builds this
Generated scripts are untrusted input. They must only ever run inside the
sandboxed worker, never in the API process.

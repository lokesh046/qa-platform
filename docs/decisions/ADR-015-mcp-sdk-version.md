# ADR-015: Use the current MCP SDK, where FastMCP is called MCPServer

Date: 2026-11-08
Status: Accepted

## Context

The brief specifies "official Python MCP SDK (FastMCP)". `pyproject.toml`
requested `mcp>=1.2.0`, which resolved to **2.3.0**.

In `mcp` 2.x, `mcp.server.fastmcp` no longer exists. Importing it raises a
`ModuleNotFoundError` carrying its own migration note: FastMCP was renamed to
`MCPServer` (`from mcp.server.mcpserver import MCPServer`), other APIs changed,
and the message suggests pinning `mcp<2` to keep v1 code running.

So the name in the brief and the name in the installed library disagree, and a
decision is needed before any code is written against either.

## Decision

Use `mcp` 2.x and its `MCPServer`. `pyproject.toml` now requires `mcp>=2.3.0`.

The brief's "FastMCP" is read as naming the SDK's high-level server API — the
thing that lets you write a decorated function and get a tool — rather than an
identifier that must appear in the source. That API still exists; it has a
different name.

## Alternatives considered

**Pin `mcp<2` and use FastMCP as written.** The brief would match the code
literally, and every tutorial and example online currently uses the v1 names.
Rejected: starting a new project on a superseded major version means a
migration is owed before the first feature is added, and the SDK's own error
message treats the pin as a stopgap, not a choice. Phase 2 onwards will want
current APIs.

**Use `mcp` 2.x but alias it — `FastMCP = MCPServer` — so the source reads as
the brief does.** Rejected quickly. It makes the code lie about the library it
uses, and the next person greps for `MCPServer` in the SDK docs and finds
nothing in this repository.

## Consequences

- Two API differences bit during Step 7 and are worth recording, because both
  fail in ways that do not look like version problems:
  - `CallToolResult.isError` is now `is_error`. The old name raises
    `AttributeError`, which in a test reads as a broken assertion.
  - Tool failures must raise `ToolError` from
    `mcp.server.mcpserver.exceptions`. Any other exception is treated as a
    crash: the model receives only `Error executing tool <name>` with the real
    message discarded, and the server logs a traceback at ERROR. Raising
    `ValueError` therefore silently threw away every user-facing message in
    this module and misfiled expected refusals as crashes.
- `mcp>=2.3.0` is pinned as a floor, so a `uv sync` cannot quietly fall back to
  a v1 release that would fail at import.
- References to "FastMCP" in the brief, in ADR-004 and in the README now
  describe something with a different name. The README and ADR-004 carry a note;
  the brief is the user's document and is left alone.
- `MCPServer.run()` takes `transport="stdio" | "sse" | "streamable-http"`. This
  project calls `run_stdio_async` and `run_streamable_http_async` directly
  instead, because it owns the event loop in order to close the backend HTTP
  client on shutdown.

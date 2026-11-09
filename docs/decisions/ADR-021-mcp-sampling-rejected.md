# ADR-021: MCP sampling was considered and rejected

Date: 2026-11-09
Status: Rejected

## Context

The direction for Phase 2 was briefly "use GitHub Copilot": generate the BRD
with the model the caller already pays for, rather than buying inference. The
mechanism for that is **sampling** — `sampling/createMessage`, a server-to-client
request that asks the client to run a completion. It is attractive for obvious
reasons: no credential, no per-call cost, and no new data processor for anyone
to approve.

It was designed in full before two facts killed it. Both are recorded here
because the design keeps looking attractive, and the next person to have the
idea should find this file rather than rediscover the problem.

### Fact 1: sampling is deprecated

Straight out of the SDK in this project's own `.venv`,
`mcp/server/session.py`:

```python
@deprecated("The sampling capability is deprecated as of 2026-07-28 (SEP-2577).",
            category=MCPDeprecationWarning)
async def create_message(...)
```

`LATEST_PROTOCOL_VERSION` is `2026-07-28`, so this is the current spec, not a
future one. It remains functional during a deprecation window of at least
twelve months. Building a phase's core feature on it would mean a rewrite
inside a year, scheduled by someone else.

### Fact 2: almost no client implements it

An independent survey of 24 MCP clients found tools supported by 24, elicitation
by 13, and **sampling by 2**. Cursor has an open feature request for it; Claude
Desktop does not document it. VS Code Copilot is one of the two that do.

That is decisive given the requirement that arrived at the same time: the tools
must be usable from **any** AI chat the team can reach. A feature that works in
one client is not that.

## Decision

**Sampling is not used.** No core path in this platform may depend on a client
capability beyond tools, which is the only thing every client implements.

BRD generation therefore runs server-side, which returns the question to
ADR-020's shape — a model this platform calls itself — for a better reason than
ADR-020 originally gave. The specific provider is settled separately; see
ADR-022.

## Alternatives

- **Sampling with a server-side fallback.** Use the caller's model when the
  client offers sampling, otherwise our own. Rejected: it doubles the
  generation path, makes output depend on which client someone happened to use,
  and the fallback has to exist and be tested anyway — at which point the
  sampling half earns nothing but a second code path to maintain.
- **Sampling now, migrate before the deprecation window closes.** Rejected: the
  migration is the same work as doing it right, done later, under a deadline
  nobody owns, after Phases 3 to 5 have been built on top.
- **Elicitation** for the same purpose. 13 of 24 clients, and it is for asking
  the *user* a question, not for inference. Not applicable.

## Consequences

- **The platform needs its own model credential** after all, and the cost and
  vendor-approval questions ADR-020 raised come back with it.
- **`app/llm/prompt.py` and `app/llm/prompts/` survive this reversal
  unchanged.** They assemble a prompt and name its version; who executes it is
  the caller's concern. That is the payoff for keeping prompt assembly a pure
  function with no client in it.
- **The capability floor is now an explicit rule**: tools only. It belongs in
  any review of a new MCP feature, because the appealing MCP capabilities are
  exactly the ones clients do not implement.
- **The design in this file is not wasted** if the platform ever targets one
  client deliberately — a VS Code extension, say. It is written out in the
  git history of this file's first version.
- **Nothing was built on it.** The reversal cost one ADR and no code, because
  the decision was recorded before the implementation. That is the documentation
  rule earning its keep, and worth noting the one time it is visible.

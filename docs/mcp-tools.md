# MCP tool reference

Every tool the MCP server exposes to GitHub Copilot Chat.

The server uses the official Python MCP SDK's high-level server API. In `mcp`
2.x that class is called `MCPServer`; it was named `FastMCP` in 1.x, which is
the name the project brief uses. See
[ADR-015](decisions/ADR-015-mcp-sdk-version.md).

It runs over **stdio** for Copilot Chat, which launches the process itself, and
over **HTTP** in the `mcp` container. The two have different trust models; see
Identity below and [ADR-014](decisions/ADR-014-mcp-transport-trust-and-identity.md).

## Conventions

- **The server is a gatekeeper.** Each tool resolves the caller, validates its
  arguments, calls one backend endpoint, writes an audit record, and shapes the
  result for a chat reply. No business logic, no database access, no Jira
  credentials.
- **Validation is strict.** Argument models set `extra="forbid"`. Invalid input
  is rejected in the MCP server before any network call, and the rejection is
  still audited.
- **Read or write.** Every tool is labelled. Phase 1 is read-only; the first
  write tool arrives with the `defects` module in Phase 5.
- **Errors.** A tool raises `ToolError`, whose message reaches the model. Any
  other exception is treated by the SDK as a crash: the model sees only
  `Error executing tool <name>` and the server logs a traceback. A tool never
  returns a backend stack trace, a token, or an internal URL. This is not a
  style rule — raising anything else silently discards the message. The tests
  in `tests/unit/test_mcp_tool.py` drive the tool through the SDK's in-process
  client precisely so that a regression here fails a test rather than shipping.
- **Auditing.** Every invocation is recorded, whatever the outcome. What is
  recorded is the actor, the tool, the outcome, and a short machine-readable
  reason — plus the issue key as `resource_id` once it has been validated. A
  *rejected* argument records the rule that rejected it, never the submitted
  value: argument text is model-supplied free input, and ADR-009 forbids
  storing that verbatim.
- **Two rows per call.** The tool invocation is recorded with `source=mcp`;
  the backend records the data access separately with `source=api`. They share
  a `request_id`. Anyone counting reads must filter on `source` or they will
  double-count.

## Identity

Who a call is *for* decides what it may read, so this is the security-critical
part of the server.

| Transport | Actor comes from | Also required |
| --- | --- | --- |
| `stdio` | `QA_ACTOR_EMAIL` in the environment | nothing; the process boundary is the identity |
| `http` | the `X-Actor-Email` header, per request | `Authorization: Bearer <INTERNAL_API_TOKEN>`, checked first |

On `http`, `QA_ACTOR_EMAIL` is **ignored entirely** — not used as a fallback.
A header is client-supplied input, so the service token is what makes the actor
claim trustworthy. On `stdio` the server refuses to start without
`QA_ACTOR_EMAIL`, rather than failing on the first tool call inside Copilot
Chat where the error would be unexplained.

Whichever transport it arrives on, the address is stripped and lowercased, so
one person is one actor in the audit trail — see
[ADR-017](decisions/ADR-017-canonical-actor-email.md).

Note the residual risk: whoever holds the service token can claim to be any
registered user. ADR-014 records it and why Phase 1 accepts it.

## Tools

### get_jira_issue

**Purpose:** "What are the acceptance criteria for ABC-123?"

**Read or write:** read

**Arguments:**

| Name | Type | Required | Rule |
| --- | --- | --- | --- |
| `issue_key` | string | yes | Must match `^[A-Z][A-Z0-9]+-[0-9]+$`, 3–64 characters |

No other argument is accepted; `extra="forbid"` rejects one rather than
silently dropping it, because a silently ignored argument becomes a
confidently wrong answer from the model.

**Validation:** the pattern above, applied in the tool body by
`mcp_server.schemas.GetJiraIssueArgs`. On failure the tool returns:

> `'abc-1'` is not a valid Jira issue key. Expected an uppercase project key, a
> hyphen, then digits, for example ABC-123.

The regex is deliberately **not** declared on the tool's input schema. The SDK
would then reject a bad key outside the handler, and no audit record would be
written — and a run of malformed calls from one actor is exactly what the audit
trail exists to show. The schema still carries a description and examples, so
the model knows the format.

**Backend endpoint:** `GET /api/v1/issues/{issue_key}`, with
`Authorization: Bearer <INTERNAL_API_TOKEN>`, `X-Actor-Email`, and
`X-Request-ID` so both processes' logs and both audit rows correlate.

**Returns:** `issue_key`, `project_key`, `summary`, `acceptance_criteria`,
`issue_type`, `status`, `jira_updated_at`, `from_cache`.

Not returned, although the backend has them: `description` and `fetched_at`.
Never available at all: comments, attachments, assignees, reporters — the cache
does not store them (ADR-009).

**Audited as:** `mcp.get_jira_issue`, `source=mcp`.

| Outcome | `outcome` | `detail` | `resource_id` |
| --- | --- | --- | --- |
| Issue returned | `allowed` | `project_key`, `from_cache` | the issue key |
| Argument rejected | `denied` | `reason: issue_key:<rule>` | null |
| Backend refused or failed | `error` | `reason: backend_refused`, `project_key` | the issue key |
| Bug in this server | `error` | `reason: internal_error`, `error_type` | the issue key |

A call refused **before** the actor is known — no token, no actor header, no
`QA_ACTOR_EMAIL` — is logged but **not** audited. There is no actor to
attribute the record to, and the backend rejects a row without one.

**Messages the model may see:**

| Cause | Message |
| --- | --- |
| Backend 404 | `ABC-1 was not found, or you do not have access to its project. Ask a QA lead to grant you access if you believe it exists.` |
| Backend 403 | the backend's own message, e.g. `This account is deactivated.` |
| Backend 401 | `This MCP server is not authorised to call the QA platform.` |
| Backend 429 | `Jira is rate limiting the QA platform. Try again shortly.` |
| Backend 502 | `The QA platform could not reach Jira. A QA lead should check its configuration.` |
| Backend 504 | `Jira did not respond in time. Try again in a moment.` |
| Backend unreachable | `The QA platform is unreachable. Check that the backend is running.` |

The 404 message deliberately covers both "no such issue" and "no access to that
project". The backend does not distinguish them
([ADR-010](decisions/ADR-010-issue-read-authorisation.md)) and this message must
not appear to either. The tool description tells the model not to retry on it.

**Implemented in:** `mcp_server/server.py`

<!--
Template for each entry:

### <tool_name>

**Purpose:** one sentence, phrased as the chat user would ask for it.
**Read or write:** read | write
**Arguments:** name, type, and the exact validation rule.
**Validation:** the regex or bound, and the message returned when it fails.
**Backend endpoint:** METHOD /api/v1/...
**Returns:** the fields in the reply.
**Audited as:** the action name written to audit_logs.
-->

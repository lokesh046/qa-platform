# Step 10: Remote access — started, then parked

Date: 2026-11-09
Phase: 2

## Goal

Make the tools usable from any AI chat, including browser-based ones, which
need a URL and therefore authentication that identifies a person rather than a
peer.

**Status: parked after the first part, deliberately.** This log exists because
a step that stopped halfway is exactly the kind of thing that becomes a mystery
in three months.

## What was done

| Path | Why |
| --- | --- |
| `docs/decisions/ADR-022-…` | The design: this server becomes an OAuth 2.1 authorization server with dynamic client registration, GitHub performs the human login, and the access token carries the verified email. Includes why GitHub cannot simply *be* the authorization server. |
| `mcp_server/config.py` | `MCP_AUTH_MODE`, `MCP_PUBLIC_URL`, the GitHub app credentials, the token signing key, `MCP_REDIS_URL` and token lifetimes, with a model validator that refuses to start in OAuth mode without the three values it needs. |
| `mcp_server/tokens.py` | Minting and verifying this server's own tokens. Pure: no Redis, no HTTP. |
| `tests/unit/test_tokens.py` | 36 tests, one per refusal path. |
| `tests/unit/test_mcp_auth.py`, `test_mcp_tool.py` | The existing http tests now state `mcp_auth_mode="service_token"`. They encode the pre-ADR-022 trust model and should have to say so. |

## How it works

Also delivered, and working: **the server is registered with Claude Code**.

```bash
claude mcp add qa-platform --scope user -- \
  uv run --directory /Users/yeswanth/Documents/jira_mcp python -m mcp_server.server
```

No secret goes into the client config. `McpSettings` has `env_file=".env"`,
which resolves against the working directory, and `uv run --directory` sets it.
Verified by launching the server with an empty environment: it listed
`get_jira_issue` and returned live Jira data. The same approach works for
Cursor and Claude Desktop.

### Why it was parked

The remaining work — the GitHub login round trip, dynamic client registration,
authorization codes in Redis, and replacing the actor header with the token —
only buys one thing: **access from a browser-based chat**. Desktop clients
already work over stdio, and stdio is the safer path by construction, because
the process boundary is the identity.

Asked directly whether browser access was wanted now, the answer was no. So the
step stops with its decision recorded and its foundation committed and tested,
rather than half-built.

## How to run / verify

```bash
claude mcp list        # qa-platform: ... - ✔ Connected
uv run pytest -q       # includes 36 token tests
```

The OAuth values can be present in `.env` with no effect: `uses_oauth` is false
unless `MCP_TRANSPORT=http` **and** `MCP_AUTH_MODE=oauth`.

```bash
uv run python -c "
from mcp_server.config import load_settings
s = load_settings(); print(s.mcp_transport, s.mcp_auth_mode, s.uses_oauth)"
# stdio oauth False
```

## Decisions and trade-offs

**A named transitional mode rather than a flag day.** `MCP_AUTH_MODE=service_token`
keeps the pre-ADR-022 behaviour so `docker compose up` works without a GitHub
OAuth app. It trusts the actor header, which is why it is named for what it
does and logged at startup. The honest cost: the hole ADR-014 identified is now
closed by *configuration* rather than by code, and nothing stops a deployment
choosing the wrong value.

**Signed tokens rather than stored ones.** Verification needs no lookup, so a
tool call does not depend on Redis. The price is no revocation before expiry,
which is why lifetimes are short and why the backend re-checks authorisation on
every call. A token proves **who**, never **what they may do**.

**Stopping was the right call.** The work was reversible, the foundation is
useful on its own — `tokens.py` is needed by any token scheme — and the
alternative was building an OAuth server for a client nobody had asked to use.

## Security notes

- **Nothing changed about today's exposure.** The HTTP transport still binds
  `127.0.0.1` and still accepts the shared token plus an actor header. The hole
  is documented, not closed.
- **`tokens.py` is the one piece where a mistake is a hole rather than a bug**,
  so it is pure and exhaustively tested: a token signed with another key, a
  tampered payload, a refresh token presented as an access token, the exact
  expiry boundary, eight malformed shapes, and a signed-but-incomplete payload.
- **No part of the signing key appears in a token.** Asserted, not assumed.
- **The validators do not echo what they reject.** Checked through
  `format_validation_error`: a 5-character signing key is reported by length
  (ADR-016).
- **Two new secrets are in `.env.example` as empty values**, with comments
  saying which is which. Three GitHub-adjacent secrets now exist — the OAuth
  app secret, the models token, and the Jira token — and confusing them is a
  realistic mistake.

## Known issues / TODO

- **Browser-based chats cannot connect.** That was the point of the step, and
  it is not done. Remaining: `authorize()` returning a GitHub redirect, the
  `/auth/github/callback` route, the primary-verified-email lookup, Redis
  storage for codes and registered clients, `resolve_actor` reading
  `get_access_token().subject`, and tests with GitHub mocked.
- **`X-Actor-Email` is still trusted on http.** Unchanged from Step 7.
- **A public URL is a separate step**, with its own review: a tunnel or a real
  host, TLS, and the GitHub app's callback pointing at it.
- **`mcp_server/tokens.py` is unused.** Nothing imports it yet. It is tested
  and committed, and it will be dead code until the step resumes.
- **Claude Desktop and Cursor are undocumented** because neither is installed
  on the development machine, so any config snippet would be untested.

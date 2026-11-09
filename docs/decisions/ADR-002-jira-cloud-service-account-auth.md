# ADR-002: Jira Cloud with a read-only service account

Date: 2026-11-08
Status: Accepted

## Context

Step 6 reads Jira issues. Jira Cloud and Jira Data Center authenticate
differently and expose different REST API versions, so the choice changes the
client code, the environment variables, and the tests. Deciding now avoids
reworking the `jira` module later.

We also had to decide whose credentials the platform uses: a service account,
or each user's own Jira token.

## Decision

1. **Jira Cloud only.** Auth is HTTP Basic with the service account's email as
   the username and an Atlassian API token as the password. The endpoint is
   `GET /rest/api/3/issue/{issueIdOrKey}`.
2. **One shared read-only service account**, not per-user credentials. Its
   token comes from `JIRA_API_TOKEN` in the environment.
3. **The platform enforces its own access control.** Because every Jira read
   uses the same service account, Jira's own permissions cannot distinguish our
   users. The `user_project_access` table (Step 5) decides whether the calling
   actor may see a given project key, and that check runs before the Jira call.
4. **Issues are cached in `jira_issues`** with `JIRA_CACHE_TTL_SECONDS`
   freshness, so repeated Copilot calls do not hit Jira's rate limits.

## Alternatives

- **Jira Data Center / Server**, using a Bearer personal access token against
  `/rest/api/2`. Rejected: the organisation is on Jira Cloud.
- **Supporting both behind a `JIRA_DEPLOYMENT` switch.** Rejected for Phase 1.
  It doubles the auth paths and the test matrix to serve a deployment we do not
  have. If a second deployment ever appears, the `jira` service is the single
  place that changes.
- **OAuth 2.0 (3LO) with per-user consent.** This is the right long-term answer:
  Jira would then enforce permissions per user and the audit trail would carry a
  real Jira identity. Rejected for Phase 1 because it needs a token store, a
  refresh loop, and a browser consent flow, none of which fit a stdio MCP server
  talking to Copilot Chat.
- **Per-user API tokens supplied by the caller.** Rejected: it would mean users
  handing personal tokens to an internal service, which we do not want to store.

## Consequences

- The service account's project visibility is an upper bound on what the
  platform can ever read. Granting it broad access is convenient and also means
  our own `user_project_access` check is the only thing preventing a user from
  reading another team's issue. That check is therefore security-critical and
  needs tests, not just code.
- `JIRA_API_TOKEN` is a long-lived credential. It must live only in the
  environment, must never be logged, and should be rotated on a schedule. The
  audit log records which actor caused a Jira read, which is what makes a shared
  credential traceable.
- Pinning to API v3 means issue descriptions arrive as Atlassian Document
  Format, not plain text. The `jira` service converts ADF to text when it
  extracts the summary and acceptance criteria; Step 6 owns that conversion.
- Moving to OAuth later changes `app/modules/jira/service.py` and adds a token
  store. The MCP tool contract does not change, so Copilot users would not
  notice.

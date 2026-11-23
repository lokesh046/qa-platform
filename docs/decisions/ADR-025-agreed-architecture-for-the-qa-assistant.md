# ADR-025: The agreed architecture, and which earlier decisions it replaces

Date: 2026-11-23
Status: Accepted
Supersedes: ADR-002, ADR-022; amends ADR-014 and ADR-024

## Context

A revised brief settled the product. It reverses the direction taken earlier
the same week, when the platform was being redesigned as a single local MCP
server with a SQLite store and generation performed by the connected client.
That design was planned in full (`openspec/changes/document-pipeline`, commit
`c830049`) and never built.

Recording it matters because four ADRs were written under the old assumptions
and would otherwise be read as current.

The product is an "AI QA Assistant": a QA engineer types a slash command in
Copilot Chat and the assistant produces a BRD, then an FDS, then Jira stories,
tasks, subtasks and test cases, then Python automation scripts — with a person
approving each stage before the next begins.

## Decision

**Two processes, as originally designed.** `mcp_server/` stays a thin
gatekeeper: validate the user, validate arguments, check the role, forward,
audit. `app/` holds every decision, backed by PostgreSQL, SQLAlchemy and
Alembic. The local-MCP-with-SQLite design is abandoned, not parked.

Six choices follow, each settled with the user:

1. **Jira OAuth is the identity.** Each person connects their own Jira account
   by browser consent; every Jira call uses their token, so Jira enforces
   their permissions. There is no second SSO login: the Jira identity is the
   platform identity, and roles hang off it.
2. **Documents live in Git, indexed by a row.** A `documents` row records the
   type, version, status, author and `git_commit`; the text lives in the
   commit. Git answers "what changed"; the row answers "which version is
   approved, by whom, derived from what" — which Git answers badly.
3. **Commits are made as the approving user**, so `git log` attributes a
   document to the person who approved it rather than to a bot.
4. **OpenSpec documents live in `openspec/`**, the location its CLI reads, so
   `openspec validate` works on generated documents. The brief said
   `specs/openspec/`; that path would be invisible to the tool.
5. **The model is GitHub Models** (ADR-023), unchanged.
6. **Review and approval happen in chat**, through tools. See the risk below.

## Alternatives

- **The local MCP server with a SQLite store**, planned in
  `openspec/changes/document-pipeline`. Rejected by the brief. It had real
  merits — one-line install, no deployment, no model credential, and the
  client doing the writing — and it could not do the things this product
  needs: unattended work, a shared audit trail, roles, and a team seeing each
  other's documents.
- **A separate SSO provider (Entra ID) on top of Jira OAuth.** Stronger: real
  conditional access, deprovisioning, group-derived roles. Rejected for now as
  a second browser login and a tenant admin, to prove an identity the Jira
  login already proves.
- **Keeping the service account** (ADR-002). Rejected: it made the service
  account's visibility an upper bound on everything, and left this platform's
  own grant table as the only thing standing between a user and another team's
  issues. Per-user OAuth removes both, which ADR-002 itself called the right
  long-term answer.
- **Our MCP server as its own OAuth authorization server** (ADR-022). Built as
  far as the token-signing core. Rejected here: it exists to let a browser-based
  chat connect to a remote MCP server, and this product is launched locally by
  VS Code. The code stays; `mcp_server/tokens.py` is unused until remote access
  is wanted.

## Consequences

- **ADR-002 is superseded.** No shared Jira service account.
  `JIRA_SERVICE_EMAIL` and `JIRA_API_TOKEN` are replaced by per-user tokens in
  `jira_connections` once build step 4 lands.
- **ADR-022 is superseded** as a product decision. Its analysis of Atlassian's
  OAuth remains correct and is the basis for step 4.
- **ADR-024 is amended.** It said a generated document is a database row and
  not a file, because the API's filesystem is ephemeral and an API that
  commits to Git is a lot of new risk. The brief requires Git, so both: the
  file is the content and the row is the index. The risk ADR-024 named is
  real and is now accepted — the backend needs a working copy and a Git
  identity, which build step 5 must solve rather than assume.
- **ADR-014's identity hole closes differently than planned.** Not by this
  server issuing tokens, but by there being no actor header to forge: the
  Jira connection identifies the user.
- **ADR-021 stands and is strengthened.** MCP sampling is still rejected; the
  model is called server-side, which also makes unattended work possible.
- **ADR-018's "no SQLite" rule is uncontested again**, since the local store
  is abandoned.
- **`user_project_access` becomes redundant** when step 4 lands, because Jira
  will enforce project visibility. It stays until then, because removing it
  now would leave the working `get_jira_issue` path with no access check at
  all.
- **Approval in chat is an honour system, and should be named as one.** A tool
  the model can call is a tool the model can call without being asked — and
  approval is the gate on writing to Jira. The mitigations: every approval is
  a row naming the decider, the version and the time; nothing is written to
  Jira without one; and the approval tools should use MCP **elicitation**, so
  the client prompts the person directly rather than the model deciding on
  their behalf. Elicitation is supported by VS Code and is in the SDK. It
  reduces the risk; it does not remove it, because the session speaks for the
  user by construction.
- **The superseded plan is deleted, not archived.** Leaving a detailed,
  contradictory plan in `openspec/changes/` is worse than removing it; it is
  recoverable in full at commit `c830049`.

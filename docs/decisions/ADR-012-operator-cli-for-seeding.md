# ADR-012: Users and grants are seeded by a CLI, not an API

Date: 2026-11-08
Status: Accepted

## Context

ADR-010 gate 2 requires a row in `users`, and gate 3 a row in
`user_project_access`. Step 5 created both tables and nothing that writes to
them, so after Step 5 every call to the issue endpoint was correctly refused
and the feature could not be demonstrated at all.

Something has to create the first user. The brief defers role-based approval
flows to Phase 3, so whatever is built now must not require an authorisation
model that does not exist yet.

## Decision

A command-line tool, `app/cli.py`, run as `python -m app.cli`:

```
add-user --email --name [--role]
grant-access --email --project
revoke-access --email --project
deactivate-user --email
activate-user --email
list-users
```

It uses the same `Settings`, engine and models as the application, so there is
no second copy of the connection logic or the validation rules. It normalises
email to lowercase and project keys to uppercase before writing, matching the
database constraints, so an operator gets a readable message instead of a
`CHECK` violation. Repeated commands are idempotent and print what they found.
Failures exit non-zero, so it is safe inside a provisioning script.

It runs on the host or in the container:

```
uv run python -m app.cli list-users
docker compose exec api python -m app.cli list-users
```

## Alternatives considered

**Admin endpoints under `/api/v1/admin`.** The natural answer, and what this
will probably become. Rejected for Phase 1 because it needs the thing the brief
defers: a way to decide who is an administrator. The service token is held by
the MCP server, so token-only protection would let any MCP caller grant
themselves access to every project — which would make ADR-010's entire gate
chain decorative. Enforcing `UserRole.ADMIN` instead needs the role to be
trustworthy, and nothing populates roles yet. Chicken and egg; the CLI breaks
it.

**A documented `psql` snippet in the step log.** Zero code. Rejected: `INSERT`
statements run by hand are unreviewable, easy to get subtly wrong — a
mixed-case email violates `ck_users_email_lowercase`, and a lowercase project
key violates `ck_user_project_access_project_key_format` — and awkward to run
inside the container. Granting project access is a security-relevant action and
deserves something auditable and repeatable.

**Auto-create a user row on first authenticated use.** Very convenient, and
common in internal tools behind SSO. Rejected: the actor email arrives in a
header that the MCP server sets, so auto-creation would mean anything holding
the service token can provision accounts by choosing a header value. A user row
with no grants cannot read anything, so the immediate blast radius is small —
but it makes `users` a list of everyone who ever sent a header rather than a
list of people who were approved, and it removes the one place where a human
currently decides.

**A seed fixture applied by Alembic.** Keeps setup in one command. Rejected:
migrations are schema, not data, and a migration that inserts a user would run
in every environment including production.

## Consequences

- Granting access needs shell access to the host or container, which is already
  the boundary protecting the database. That is the intended trade: provisioning
  is a deliberate act by someone who can already reach the data.
- There is no self-service and no audit row for a grant. `user_project_access`
  records `granted_by_id`, but the CLI has no way to know who is running it, so
  it writes `NULL`. Worth fixing when admin endpoints arrive.
- Phase 3 will likely replace this with endpoints. The CLI should stay anyway:
  bootstrapping the first administrator always needs a path that does not
  require an administrator.
- The CLI has no committed test, and it is the only thing that can grant
  access. Recorded in the Step 6 known issues.

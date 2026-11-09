# app/modules

One folder per business capability. Every implemented module has exactly these
four files:

| File            | Holds                                                      |
| --------------- | ---------------------------------------------------------- |
| `router.py`     | FastAPI endpoints. Parse, call the service, return. No logic. |
| `service.py`    | The business logic. Knows nothing about HTTP.               |
| `schemas.py`    | Pydantic v2 request and response models.                    |
| `repository.py` | Database queries. The only place SQLAlchemy is used.        |

## Direction of calls
`router -> service -> repository`. Never the other way round, and a repository
never imports a service.

## Implemented
`health` — `/api/v1/health` and `/api/v1/ready` (Step 4). No `repository.py`:
it owns no tables, so there is nothing to put behind one. Its data sources are
the probe helpers in `app/core/database.py` and `app/core/redis.py`. An empty
file to satisfy the table above would be worse than this note.

`jira` — `GET /api/v1/issues/{issue_key}` (Step 6). Has two files the table
above does not name: `client.py`, the HTTP gateway to Jira, because a
repository talks to *our* database and these share no failure modes; and
`adf.py`, pure functions converting Atlassian Document Format to text, isolated
so the trickiest parsing in the project is testable with a literal dict.

`audit` — the append-only trail (Step 6 addendum; `router.py` added in Step 7).
`POST /api/v1/audit` exists so the MCP gatekeeper can record its tool calls
without holding database credentials; there is still no read endpoint, because
reading the trail is an administrator's job done with SQL. Its
`service.record` deliberately opens its **own** session rather than taking the
caller's, because `get_session` rolls back on exception and a denial is
reported by raising — see
[ADR-013](../../docs/decisions/ADR-013-audit-write-transaction-and-failure.md).

## Phase 1 (implemented in later steps of Phase 1)
`users`, `jobs`

## Later phases (placeholders, see each folder's README)
`brd`, `testgen`, `approvals`, `automation`, `execution`, `reports`, `defects`

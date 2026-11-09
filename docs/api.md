# HTTP API reference

Every endpoint the FastAPI backend exposes. Base prefix `/api/v1`, configurable
via `API_V1_PREFIX`.

## Conventions

- **Response envelope.** Every response, success or failure, is
  `{"data": ..., "error": ..., "request_id": "..."}`. On success `error` is
  `null`; on failure `data` is `null` and `error` is
  `{"code": "...", "message": "...", "details": ... | null}`. Implemented in
  `app/core/errors.py`.
- **Authentication.** Internal callers send
  `Authorization: Bearer <INTERNAL_API_TOKEN>` and `X-Actor-Email: <email>`.
  The token is verified first; the actor header is read only afterwards, so an
  unauthenticated caller cannot choose whose name reaches the audit log. See
  `docs/decisions/ADR-003-mcp-to-backend-auth.md`.
- **The actor email is canonicalised.** It is stripped and lowercased before
  anything else happens, so `QA.One@Acme-QA.co` and `qa.one@acme-qa.co` are the
  same actor in the audit trail. `users.email` permits only the lowercase form,
  so nothing is lost. See
  [ADR-017](decisions/ADR-017-canonical-actor-email.md).
- **Request IDs.** The backend accepts an inbound `X-Request-ID` and echoes it
  in both the response header and `request_id`; if absent, or if the value is
  not printable alphanumerics plus `-_.` within 64 characters, it generates
  one. The same value appears in every log line for the request.
- **Roles.** `public` needs no auth. `service` needs a valid service token.
  `service + project access` additionally requires the actor to have access to
  the project the resource belongs to.
- **Input validation.** All request models set `extra="forbid"`, so an unknown
  field is a 422, not a silently ignored key. A 422 body lists the offending
  fields under `error.details.fields`, but never echoes the submitted value.

## Error codes

`error.code` is a stable identifier. Clients may branch on it.

| Code | Status | Meaning |
| --- | --- | --- |
| `validation_error` | 422 | Input was malformed or unacceptable. |
| `unauthorized` | 401 | Missing or invalid service token, or missing/invalid actor header. |
| `forbidden` | 403 | Authenticated, but not allowed to do this. |
| `not_found` | 404 | No such resource, or the actor may not see it. |
| `conflict` | 409 | Clashes with current state. |
| `rate_limited` | 429 | A rate limit was reached. Not yet raised by anything. |
| `upstream_error` | 502 | Jira answered, but not usefully. |
| `upstream_timeout` | 504 | Jira did not answer in time. |
| `internal_error` | 500 | Unexpected failure. The body never contains details; quote `request_id`. |
| `service_unavailable` | 503 | A dependency this service needs is unreachable. Only `/ready` raises it. |

## Endpoints

### GET /api/v1/health

**Purpose:** liveness. Report that this process is running.
**Required role:** public. A Kubernetes probe and a Compose healthcheck cannot
carry a token, and nothing here is secret.
**Path / query parameters:** none.
**Request body:** none.
**Success response:** 200. `data` is a `HealthRead`:

| Field | Type | Notes |
| --- | --- | --- |
| `status` | `"ok"` | Constant. A dead process cannot answer. |
| `service` | string | `APP_NAME`. |
| `version` | string | `app.__version__`. |
| `environment` | `local` \| `ci` \| `staging` \| `production` | `APP_ENV`. |

**Error responses:** none in normal operation. This endpoint touches no
dependency and does not fail while the process can answer. Do not add a
dependency check to it: a failing liveness probe causes a container restart,
and a brief database outage would restart every instance at once. See ADR-007.
**Implemented in:** `app/modules/health/router.py`

### GET /api/v1/ready

**Purpose:** readiness. Report whether every dependency needed to serve traffic
is reachable.
**Required role:** public, as above.
**Path / query parameters:** none.
**Request body:** none.
**Success response:** 200 when every probe passes. `data` is a `ReadinessRead`:

| Field | Type | Notes |
| --- | --- | --- |
| `status` | `"ready"` \| `"not_ready"` | Aggregate. `"ready"` on a 200. |
| `checks` | array of `DependencyCheck` | One per dependency, fixed order: `database`, then `redis`. |

Each `DependencyCheck`:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | `database` or `redis`. |
| `status` | `ok` \| `failed` | Outcome of the probe. |
| `duration_ms` | number | How long the probe took. Distinguishes unreachable from slow. |
| `target` | string \| null | The host actually reached, password masked. `null` in `staging` and `production`. |

**Error responses:**

| Status | `error.code` | When |
| --- | --- | --- |
| 503 | `service_unavailable` | At least one probe failed. |

On a 503 the check list moves from `data` to `error.details`, because a non-2xx
response in this API always has a null `data`. `error.details` holds the whole
`ReadinessRead`, so `error.details.checks` tells you which dependency failed.
`error.message` names the failed dependencies.

**Implemented in:** `app/modules/health/router.py`

### GET /api/v1/issues/{issue_key}

**Purpose:** return one Jira issue's summary, description and acceptance
criteria. Served from the local cache when fresh, otherwise fetched from Jira
and cached.

**Required role:** service token **and** an actor header naming an active user
who holds a `user_project_access` grant for the issue's project. Any
platform role is sufficient; the grant is what matters.

**Headers:**

| Header | Required | Notes |
| --- | --- | --- |
| `Authorization` | yes | `Bearer <INTERNAL_API_TOKEN>`. Compared in constant time. |
| `X-Actor-Email` | yes | The end user the MCP server is acting for. |

**Path parameters:**

| Name | Type | Validation |
| --- | --- | --- |
| `issue_key` | string | Must match `^[A-Z][A-Z0-9]+-[0-9]+$`, 3–64 characters. For example `ABC-123`. |

**Request body:** none.

**Success response:** 200. `data` is an `IssueRead`:

| Field | Type | Notes |
| --- | --- | --- |
| `issue_key` | string | As requested. |
| `project_key` | string | Derived from the key by the database, not by the caller. |
| `summary` | string | Falls back to the issue key if Jira returns no summary. |
| `description` | string \| null | ADF flattened to plain text. |
| `acceptance_criteria` | string \| null | From `JIRA_ACCEPTANCE_CRITERIA_FIELD` if set and non-empty, else from an "Acceptance Criteria" heading in the description, else null. See ADR-011. |
| `issue_type` | string \| null | For example `Bug`. |
| `status` | string \| null | For example `To Do`. |
| `jira_updated_at` | datetime \| null | Jira's own `updated`, normalised to UTC. |
| `fetched_at` | datetime | When this service last read the issue from Jira. |
| `from_cache` | bool | True if no Jira call was made. |

**Error responses:**

| Status | `error.code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing or malformed `Authorization`, wrong token, or missing `X-Actor-Email`. |
| 403 | `forbidden` | The actor is not a registered user, or the account is deactivated. Names neither the issue nor the project. |
| 404 | `not_found` | The issue does not exist, **or** the actor has no grant for its project. |
| 422 | `validation_error` | The issue key does not match the pattern. The body does not echo the submitted value. |
| 429 | `rate_limited` | Jira rate-limited this service. `details.retry_after` when Jira supplied it. |
| 502 | `upstream_error` | Jira refused this service's credentials, failed, or returned something unparseable. |
| 504 | `upstream_timeout` | Jira did not answer within `JIRA_TIMEOUT_SECONDS`. |

**The 404 is deliberately ambiguous.** "No such issue" and "you have no access
to that project" return the same status, the same `error.code`, and the same
message shape. Without that, any holder of the service token could enumerate
which projects and issues exist without holding a single grant. The denial is
recorded in the audit trail with the actor, project and issue key, so an
administrator can still answer "why can't I see this?". See
[ADR-010](decisions/ADR-010-issue-read-authorisation.md).

**Checks happen in a fixed order** — key shape, then identity, then project
access, then data. **No Jira call is made until project access has passed.**

**Audited.** Every outcome writes a row to `audit_logs` with `action =
jira_issue.read`, `source = api`, the actor's email, the issue key, and a
`detail` saying why:

| Outcome | `outcome` | `detail` |
| --- | --- | --- |
| Served from cache | `allowed` | `cache: hit` |
| Fetched from Jira | `allowed` | `cache: miss`, `refreshed` |
| Actor not registered | `denied` | `reason: actor_not_registered` |
| Actor deactivated | `denied` | `reason: actor_inactive` |
| No project grant | `denied` | `reason: no_project_access`, `project_key` |
| Jira failed | `error` | `reason: <error code>` |

A 401 writes nothing, because the actor is not yet known. A 422 writes nothing,
because a malformed key reaches no access decision. If the audit write itself
fails, the request fails with 500 and returns no data — see
[ADR-013](decisions/ADR-013-audit-write-transaction-and-failure.md).

**The order of the checks is covered by tests.** `tests/unit/test_jira_service.py`
stubs the repository with a stub that records its own call order, so "no Jira
call is made before project access has passed" is asserted rather than
reviewed.

**Implemented in:** `app/modules/jira/router.py`

**Example**

```
curl -s -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
     -H "X-Actor-Email: qa.one@example.com" \
     http://127.0.0.1:8000/api/v1/issues/ABC-123
```

```json
{
  "data": {
    "issue_key": "ABC-123",
    "project_key": "ABC",
    "summary": "Login rejects a valid password",
    "description": "A user cannot log in.",
    "acceptance_criteria": "- Valid password grants access",
    "issue_type": "Bug",
    "status": "To Do",
    "jira_updated_at": "2026-11-01T04:00:00Z",
    "fetched_at": "2026-11-08T12:04:18.621Z",
    "from_cache": false
  },
  "error": null,
  "request_id": "d176569a9cbc4a9797c1195bdf178168"
}
```

### POST /api/v1/audit

**Purpose:** append one audit record on behalf of a gatekeeper. Exists so the
MCP server can record its own tool calls without holding database credentials.

**Required role:** service token and an actor header. There is no admin role
check: the endpoint can only append, and only within the `mcp.` action
namespace.

**Headers:** `Authorization: Bearer <INTERNAL_API_TOKEN>` and `X-Actor-Email`,
as for every other endpoint. `X-Request-ID` is honoured and sanitised, so the
caller's row correlates with the backend's own.

**Request body:** an `AuditWriteRequest`. `extra="forbid"`.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | Must match `^mcp\.[a-z0-9_]+(\.[a-z0-9_]+)*$`. |
| `outcome` | `allowed` \| `denied` \| `error` | yes | |
| `resource_type` | string \| null | no | Max 32 characters. |
| `resource_id` | string \| null | no | Max 128 characters. |
| `detail` | object | no | **Scalar values only** (string, number, bool, null). A nested object or array is rejected. Never a credential or a payload; see ADR-009. |

**Four fields are set by the server and cannot be supplied:**

| Field | Source | Why not the body |
| --- | --- | --- |
| `actor_email` | the `X-Actor-Email` header | the same value the rest of the API authorises against |
| `source` | always `mcp` | otherwise a caller could write a row claiming to be the backend |
| `occurred_at` | the column's `server_default now()` | a record cannot be backdated |
| `request_id` | this request's own context | already sanitised by the middleware |

The `mcp.` namespace constraint matters for the same reason: without it,
anything holding the service token could append a convincing
`jira_issue.read` row for a read that never happened.

**Success response:** 201. `data` is `{"recorded": true}`.

It deliberately carries **no audit id**. ADR-008 chose a sequential key for
this table on the grounds that nothing external quotes it; returning one would
make that false and would leak how much work the platform has done.

**Error responses:**

| Status | `error.code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing or invalid service token, or no actor header. |
| 422 | `validation_error` | Unknown field, an action outside `mcp.`, or a non-scalar `detail` value. |

**There is no read endpoint.** Reading the trail is an administrator's job,
done with SQL.

**Implemented in:** `app/modules/audit/router.py`

<!--
Template for each entry:

### <METHOD> <path>

**Purpose:** one sentence.
**Required role:** public | service | service + project access
**Path / query parameters:** name, type, validation rule.
**Request body:** schema name and fields, or "none".
**Success response:** status code and the shape inside `data`.
**Error responses:** status code, `error.code`, and when it happens.
**Implemented in:** app/modules/<module>/router.py
-->

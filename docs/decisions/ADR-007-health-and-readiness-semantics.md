# ADR-007: Separate liveness and readiness, and what each may reveal

Date: 2026-11-08
Status: Accepted

## Context

Step 4 needs `/health` and `/ready` under `/api/v1`. Three questions had to be
settled before writing them.

**1. Should either endpoint check dependencies?** A single endpoint that pings
Postgres is the common shortcut. Under an orchestrator it is actively harmful.
Kubernetes restarts a container whose *liveness* probe fails. If liveness
depends on Postgres, a thirty-second database blip restarts every API pod at
once, they all reconnect simultaneously, and the restart storm outlasts the
original fault. Readiness failure, by contrast, only removes the pod from the
load balancer, which is the correct response to an unreachable dependency.

**2. What status code does a not-ready service return?** 503 is the
conventional answer, but this API had no 503 in its error table: `UpstreamError`
is 502 and means "a dependency answered badly during your request", which is a
different statement from "do not send requests yet".

**3. May `/ready` say *which* database it reached?** During Step 3 a second
project's Postgres container was holding port 5432 while ours had no host
binding at all. Every local connection silently reached the wrong database for
some time. An endpoint that reported the host it actually connected to would
have exposed that in seconds. But `/ready` is unauthenticated — a probe cannot
carry a bearer token — so anything it prints is readable by anything that can
reach the port, and internal hostnames are reconnaissance material.

## Decision

**Two endpoints, different jobs.**

`GET /api/v1/health` is liveness. It touches no dependency, is synchronous and
pure, and returns 200 whenever the process can answer at all. It reports the
service name, version and environment.

`GET /api/v1/ready` is readiness. It probes Postgres (`SELECT 1`) and Redis
(`PING`) concurrently and reports each result with a duration. 200 when all
pass; 503 when any fails.

**A new error code.** `service_unavailable` / 503, raised as
`ServiceUnavailableError`. Kept distinct from `upstream_error` / 502 because
load balancers and orchestrators treat the two differently.

**Failures use the envelope, which moves the payload.** A not-ready response is
a non-2xx response, and this API guarantees that every non-2xx body has a null
`data` and a populated `error`. So the per-dependency check list appears under
`data.checks` on success and under `error.details.checks` on failure. The
asymmetry is documented on the endpoint and in `docs/api.md`.

**Dependency targets are shown locally and withheld in deployments.** The
`target` field carries the host actually reached, with the password masked by
`Settings.database_url_safe` / `redis_url_safe`. It is populated when
`settings.is_deployment` is false (`local`, `ci`) and `null` in `staging` and
`production`.

**Both endpoints are unauthenticated.** Neither reveals anything secret, and a
Compose healthcheck or Kubernetes probe cannot authenticate.

## Alternatives considered

**One `/health` endpoint that checks everything.** Fewer moving parts, and it is
what most tutorials show. Rejected: it conflates "restart me" with "don't route
to me", and the restart-storm failure mode above is severe and hard to diagnose
after the fact.

**Readiness returns 200 with `status: "not_ready"` in the body.** Some teams do
this so monitoring never sees a red status code. Rejected: every load balancer
and orchestrator reads the status code, not the body, so this makes the endpoint
useless for its actual purpose.

**Return the check list in `data` even on a 503.** More convenient for a human
reading `curl` output. Rejected: it breaks the envelope invariant, which exists
so a client can test `if error is not None` once and be right everywhere.

**Never expose `target`, in any environment.** The most conservative option.
Rejected because the Step 3 incident is concrete evidence of the cost: a
developer cannot debug a connection they cannot see, and `local`/`ci` has no
meaningful attacker. The password is masked in all environments regardless.

**Always expose `target`.** Rejected. An unauthenticated endpoint that prints
`db.internal.example.com:5432` and a service-account username hands an attacker
inside the network a map for free.

**Skip the Redis probe in Phase 1,** since nothing uses Redis yet. Rejected:
Redis is in `docker-compose.yml` and in `Settings`, so a misconfiguration is
possible today and would be discovered in Phase 3 instead of now. The probe is
twenty lines.

## Consequences

- A readiness failure is visible in one request, with per-dependency detail and
  a timing for each, which distinguishes "unreachable" from "slow".
- `docker-compose.yml`'s `api` healthcheck targets `/api/v1/health`, so a
  database outage no longer makes the container unhealthy. Dependent services
  therefore start even when Postgres is unwell, which is the intended behaviour
  but differs from Step 2's assumption.
- Local output contains hostnames and usernames. That is deliberate and
  environment-gated; anyone adding a field to this endpoint must decide which
  side of `is_deployment` it belongs on.
- Adding a dependency means adding one tuple to `_probe` list in
  `app/modules/health/service.py`. Forgetting to do so makes `/ready` lie,
  which is the main maintenance risk here.
- `/ready` is unauthenticated and does real network I/O, so it is a cheap
  amplification target. Not addressed in Phase 1; noted in the Step 4 log.

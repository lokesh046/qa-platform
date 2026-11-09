# ADR-005: Object storage is deferred to Phase 3

Date: 2026-11-08
Status: Accepted
Supersedes: the MinIO parts of Step 2 as originally planned

## Context

Step 2 was specified to include MinIO in the local stack, so that an
S3-compatible endpoint and the `qa-artifacts` bucket would exist before Phase 3
needed to store Playwright screenshots, traces, and video.

On the first run with a live Docker daemon, every MinIO image failed to pull:

```
minio/minio:latest                        unauthorized: authentication required
minio/minio:RELEASE.2025-04-22T22-12-26Z  unauthorized: authentication required
minio/mc:latest                           pull access denied for minio/mc
quay.io/minio/minio:latest                no such manifest
quay.io/minio/minio  (quay REST API)      HTTP 401 Requires authentication
bitnami/minio:latest                      no such manifest
```

This is repository-level gating, not a bad tag: MinIO has put its community
container images behind authentication. `bitnami/minio` is also gone, following
Bitnami's move of its catalogue to a paid namespace.

Nothing in the codebase reads or writes object storage. The first code that
will is in Phase 3, several steps away.

## Decision

**No object-storage service in the stack until Phase 3.**

- `minio` and `minio-init` are removed from `docker-compose.yml`, along with the
  `miniodata` volume and the `MINIO_ENDPOINT` override on the `api` service.
- The `MINIO_*` variables in `.env.example` are replaced by a commented-out,
  vendor-neutral `S3_*` block with a pointer to this ADR. They are commented
  rather than deleted so the shape of what Phase 3 needs stays visible.
- The provider is chosen in Phase 3, by the step that writes the first artifact,
  with a working endpoint proven by that step's tests.

Candidates that were confirmed publicly pullable on 2026-11-08, for whoever
makes that choice:

| Image | Licence | Persists | Notes |
| --- | --- | --- | --- |
| `chrislusf/seaweedfs` | Apache-2.0 | yes | Maintained. S3 API on 8333. No bucket console. |
| `dxflrs/garage` | AGPL-3.0 | yes | Purpose-built self-hosted S3. Needs a layout-assign init step. |
| `zenko/cloudserver` | Apache-2.0 | yes | Scality's S3 server. |
| `localstack/localstack` | Apache-2.0 | **no** | Familiar, S3 in the free tier, but persistence is a paid feature. ~1 GB. |
| `adobe/s3mock` | Apache-2.0 | partial | Tiny, `initialBuckets` env var. Built for tests, not storage. |
| `bitnamilegacy/minio` | Apache-2.0 | yes | Genuinely MinIO, frozen archive namespace, **no security patches ever**. |

## Alternatives

- **`bitnamilegacy/minio`.** The smallest diff by far: same environment
  variables, same web console, and `MINIO_DEFAULT_BUCKETS` would have removed
  the `minio-init` service. Rejected because the namespace is explicitly an
  archive that will never receive a security update, and an internal platform
  will eventually be read by a security reviewer who will ask about exactly
  this.
- **Pick a replacement now (SeaweedFS was the leading candidate).** Rejected on
  timing, not on merit. Choosing a storage backend with no code that calls it
  means guessing at the access pattern, writing an init container nothing
  exercises, and carrying an unused service in every developer's stack for two
  phases. The vendor situation has already changed once during this project.
- **Authenticate to MinIO's registry.** Needs a MinIO subscription or account
  credentials, which is a procurement question and not something to encode in a
  local dev stack.
- **Keep `minio` in compose behind a `profiles:` key.** Still broken, just
  broken later and less visibly.

## Consequences

- `docker compose up` is lighter: two infrastructure services instead of four.
- Phase 3 carries work that Step 2 was meant to absorb: choosing a provider,
  adding the service, creating the bucket, and naming the environment
  variables. That step should expect roughly half a day for it rather than
  treating storage as already solved.
- The variable names change from `MINIO_*` to a vendor-neutral `S3_*`. Nothing
  reads them yet, so the rename costs nothing now and avoids a misleading name
  if the provider is not MinIO.
- `app/modules/execution/` and `app/modules/reports/` will need an S3 client
  behind a small interface, so the provider can be swapped without touching
  business logic. Worth writing that interface before the first `boto3` call
  appears in a service.
- Production was never going to run MinIO-in-compose anyway; it will use
  whatever object storage the organisation already has. This decision only
  affects local development and CI.

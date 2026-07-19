# Railway Docker Volume Compatibility Design

## Context

Railway rejects the API and worker images before their builds start because both
Dockerfiles declare `VOLUME ["/data"]`. Railway's Dockerfile validator does not
support Docker-managed volumes and requires persistent storage to be configured
as Railway service volumes instead. Every API and worker deployment after commit
`3a6cbd30f22f12bb9963d3eff68b6885c4073e09` has therefore failed while the last
successful containers continued serving production traffic.

Production currently has a Railway volume only for Postgres. Both application
services use Postgres-backed platform state. This fix does not introduce shared
Drive storage: `/data` remains writable container-local storage on Railway and
is not durable across deployments.

## Decision

Remove the `VOLUME ["/data"]` instruction from `Dockerfile` and
`Dockerfile.worker`. Keep the existing creation, ownership, permissions, and
`ONEBRAIN_DATA_DIR=/data` configuration so each non-root runtime can still write
to `/data`.

Update the container hardening test to enforce the security properties the
images actually require:

- runtime processes execute as the `onebrain` user;
- `/data` is created with ownership and mode appropriate for that user;
- the Python and temporary-directory environment remains hardened; and
- neither Railway-deployed Python image declares a Docker-managed volume.

No Railway service variables or volume resources will be added or changed.

## Alternatives Considered

1. **Focused Dockerfile compatibility fix (selected).** This restores API and
   worker builds without changing runtime topology or application behavior.
2. **Attach separate Railway volumes.** Railway volumes are service-scoped, so
   separate API and worker mounts would not provide the shared Drive filesystem
   expected by the application and could create divergent data.
3. **Introduce shared object storage.** This is the correct direction if Drive
   must be durable on Railway, but it requires a new storage backend and is
   outside this deployment repair.

## Verification and Rollout

Run the focused container hardening test and the repository's relevant static
checks. Validate both Dockerfiles locally to the extent supported by the
available Docker tooling. Commit and push the isolated branch, merge it to
`main`, and push `main` according to the repository shipping workflow.

Use Railway CLI to watch the automatically triggered production builds for both
`onebrain` and `onebrain-workers`. Success requires both deployments to reach
`SUCCESS`, the API health check to pass, and the public `/health` endpoint to
respond successfully. If either service exposes a new build or startup failure,
stop and diagnose that failure rather than changing production data or secrets.


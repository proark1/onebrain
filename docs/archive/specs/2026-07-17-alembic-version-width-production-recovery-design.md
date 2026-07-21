# Alembic Version Width Production Recovery

**Date:** 2026-07-17

## Context

Railway successfully builds the current API and worker images, but the new
replicas cannot become healthy. The API reaches migration
`0025_provisioning_module_selection`, applies its transactional schema work, and
then PostgreSQL rejects Alembic's revision update because the 34-character
revision identifier does not fit the standard
`alembic_version.version_num VARCHAR(32)` column. The transaction rolls back.
Workers consequently remain on revision `0024_ai_employees_runtime` and time
out waiting for the required schema.

Railway keeps the previous healthy API and worker deployments running, so this
is a blocked rollout rather than a production outage. The admin UI deployment
is already healthy.

## Decision

Migration `0025_provisioning_module_selection` will widen
`alembic_version.version_num` from `VARCHAR(32)` to `VARCHAR(128)` as its first
operation. The published revision identifier and migration chain remain
unchanged.

This is preferred over renaming the revision because a published revision name
is persistent migration identity. It is preferred over a one-off production SQL
change because every existing or newly provisioned PostgreSQL environment must
be able to run the same repository migration without operator intervention.

## Migration Behavior

The upgrade performs the following operations in order:

1. Widen `alembic_version.version_num` to `VARCHAR(128)`.
2. Add `control_deployments.selected_module_ids`.
3. Add `provisioning_runs.module_ids`.
4. Remove the retired `provisioning_runs.bundle_id` column.

PostgreSQL can widen this column without truncation or data loss. The operation
runs inside the same transactional migration as the module-selection changes,
so a failure leaves both the schema and Alembic revision at `0024`.

The downgrade does not shrink `version_num`. Shrinking provides no functional
benefit, can reject a currently recorded long identifier before Alembic stamps
the previous revision, and would reintroduce the production failure for future
migrations.

## Validation

A focused regression test will load migration `0025`, capture its emitted SQL,
and assert that the Alembic version-column widening occurs before any product
schema mutation. Existing migration-chain and required-head assertions remain
unchanged.

Release validation includes:

- focused migration and deployment-runtime tests;
- the complete Python test corpus, with the existing Windows shell harness run
  in its validated Git Bash process;
- frontend tests, lint, type checking, and production build when affected by
  the merged branch baseline;
- staged diff and secret checks.

## Rollout and Recovery

The fix is committed on an isolated branch, pushed, merged into `main`, and
allowed to deploy through the existing Railway source integration. No manual
production SQL is executed.

The rollout is complete only when:

- the latest `onebrain` API deployment is `SUCCESS` and `/health` responds;
- the latest `onebrain-workers` deployment is `SUCCESS` rather than waiting on
  revision `0024`;
- `onebrain-admin-ui` remains `SUCCESS`;
- GitHub tests and the `release-dev` deployment succeed;
- the Railway production deployment for the fix commit reports success.

If the new deployment fails for an unrelated reason, Railway's previous healthy
replicas continue serving. The code commit can be reverted, while the widened
Alembic column is intentionally retained because it is backward-compatible.

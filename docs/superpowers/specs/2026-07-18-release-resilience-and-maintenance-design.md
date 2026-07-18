# Release resilience and host maintenance design

## Goal

Make development releases safe to retry, prevent disk exhaustion from taking
down OneBrain hosts, and complete PostgreSQL collation maintenance without
discarding current deployment state.

This work covers Mission Control and the single development gate. It does not
change customer-facing product behavior or touch unrelated Drive work already
present in the repository.

## Current constraints

- The development gate is healthy on verified release `2026.07.17.189` and
  schema revision `0025_provisioning_module_selection`.
- Candidate `2026.07.18.215` requires revision
  `0030_job_queue_rls_roles`, which needs distinct restricted PostgreSQL app
  and worker roles before Alembic runs.
- Existing host updates replace image pins only. They do not refresh sealed
  runtime credentials or role-capable host assets.
- A migration-fence failure currently leaves the application quiesced rather
  than restoring the previous healthy stack.
- Docker image accumulation previously filled the root disks on both hosts.
- PostgreSQL reports a collation-version mismatch. Refreshing the recorded
  version without rebuilding indexes would mask, rather than repair, it.

## Design

### 1. Safe release prerequisites and rollback

The updater must validate all migration prerequisites before it stops Caddy or
application services. For a role-split release, the preflight requires:

1. a compatible host-asset version;
2. a current sealed secret bundle;
3. distinct, restricted runtime role names; and
4. successful role normalization before Alembic is invoked.

The gate agent refreshes the trusted bundle before checking desired state. The
bootstrap path must use the same Compose override as the updater, so a secret
rotation cannot discard the current digest-pinned release.

If a prerequisite, migration fence, or migration command fails after an update
has begun, the updater restores `images.override.prev.yml`, restarts the prior
stack, verifies its health endpoint, and records a rolled-back outcome. A
failed candidate therefore cannot leave the gate in an indefinite “updating”
state.

### 2. Role-split host upgrade and candidate deployment

The development gate upgrade is an explicit controlled operation:

1. temporarily prevent dispatch of the pending candidate;
2. create missing runtime credentials only through Mission Control's existing
   backfill flow;
3. refresh the sealed bundle and install the compatible host assets without
   replacing the current image pins;
4. run the idempotent PostgreSQL role normalizer;
5. validate the owner, app, and worker role boundaries;
6. take a recoverable database backup;
7. deploy candidate `2026.07.18.215`; and
8. verify its health, schema revision, heartbeat, and role access before
   marking it successful.

Mission Control follows the same host-asset and role-normalization sequence
only after the development gate proves it. Mission Control's existing `.env`
is retained; no creation/bootstrap process is rerun as an upgrade mechanism.

### 3. Disk retention and storage alerts

Each host receives a root-only, low-priority daily maintenance service and
timer. It removes build cache and old unreferenced image layers only after
protecting images referenced by running containers, the current override, the
previous override, and the last verified applied state. It does not use a blind
`docker image prune -a` policy that could delete a rollback image.

Host reporters include metadata-only root and data-volume capacity values.
The fleet watchdog opens and resolves `low_root_disk` and `low_data_disk`
alerts using configurable thresholds. Fleet overview surfaces the capacity and
open alert state so an operator can act before PostgreSQL loses writable space.

### 4. PostgreSQL collation maintenance

For each host, first query every connectable database for recorded and actual
collation versions, including `onebrain`, `assistant`, `communication`, and
`template1`. Only mismatched databases are changed.

For every affected database:

1. verify adequate data-volume space and create a custom-format backup;
2. stop application traffic for a brief maintenance window while PostgreSQL
   remains running;
3. inspect explicit collations; rebuild any affected dependent objects;
4. run `REINDEX DATABASE` while connected to that database; and
5. run `ALTER DATABASE ... REFRESH COLLATION VERSION` only after reindexing.

The host stack is restarted and health, Alembic revision, and collation state
are verified after maintenance. `template0` is not modified.

## Failure handling

- Bundle refresh or host-asset installation failure leaves the current healthy
  release serving and reports a failed prerequisite.
- A migration failure restores the prior image override and healthy stack. A
  `restore_required` migration also restores the recorded database backup.
- Host-maintenance cleanup performs a dry run before deletion and never removes
  protected image references.
- A collation operation stops immediately on backup, space, reindex, or health
  failure; the verified backup is retained for recovery.

## Validation

Automated coverage includes:

- legacy role-split preflight and bundle refresh behavior;
- bootstrap preservation of `images.override.yml`;
- migration-fence rollback and prior-stack recovery;
- role privilege boundaries for app and worker logins;
- protected Docker retention selection and host renderer assets;
- reporter and watchdog storage-alert behavior; and
- collation preflight command generation and no-op behavior for matching
  versions.

Operational validation includes backups, service health, current Alembic
revision, deployment heartbeat, release version, public `/health` and
`/cockpit` responses, and confirmation that temporary maintenance access is
closed.

## Delivery order

1. Implement and test updater, bundle, host-asset, retention, telemetry, and
   alert changes.
2. Ship the code to `main`.
3. Apply compatible assets and role prerequisites to the development gate.
4. Deploy and verify `2026.07.18.215` on the gate.
5. Apply the proven upgrade to Mission Control.
6. Run the separate verified collation maintenance window on each host.

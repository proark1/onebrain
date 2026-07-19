# Existing Development Gate Full-Stack Conversion

## Purpose

Convert the already-enrolled development gate `onebrain_development_gate` on `91.98.26.173` from its legacy three-service Core topology to the exact eight-service development release-gate topology. The operation must keep the current Hetzner server, deployment identity, IP addresses, DNS name, fleet enrollment, and encrypted secret bundle. It must not create, replace, rebuild, resize, or delete a Hetzner server.

The target is candidate `2026.07.18.311`, migration `0034_drive_malware_quarantine`, with these modules:

1. `onebrain-api`
2. `onebrain-admin-ui`
3. `onebrain-workers`
4. `assistant-service`
5. `communication-api`
6. `communication-widget`
7. `communication-voice`
8. `communication-workers`

## Current State

- Mission Control runs build `2026.07.18.315` at revision `88ad956e071d057913278e75d85769229d2e25ee` and is healthy.
- The development gate runs legacy release `2026.07.18.223` and reports only the three Core modules.
- The gate has applied secret epoch `3`, including the prepared Assistant and Communication integration credentials.
- Candidate `2026.07.18.311` is registered but is in `dev_failed` after the repaired preflight returned `development_gate_replacement_required`.
- The current host Compose file defines only the Core application services, so merely relaxing the preflight would produce an incomplete and unverifiable rollout.
- The user-management polling agent is absent. The previously queued directory job expired without being leased.

## Selected Approach

Perform a controlled, backup-first topology conversion on the existing host, then let Mission Control verify the already-deployed candidate through its normal signed rollout path.

The conversion uses the repository's deterministic production renderer at revision `88ad956` to generate the Compose file, Caddy configuration, per-service environment templates, host scripts, profiles, and module allowlist for the existing deployment identity. Secret values are never rendered locally or printed. The existing encrypted epoch-3 bundle remains the source of runtime secrets.

The operation stages every replacement asset before activating it. It pulls all immutable images before downtime, runs the product migrations through the rendered one-shot migration services, starts the full stack, and verifies exact module health before retrying promotion.

## Immutable Target Images

- `onebrain-api`: `sha256:3f458c25dc2e5c1e9f9a864812a44b668df4adca33cb72ef472bf95bd99319c0`
- `onebrain-workers`: `sha256:b03b72ee6492503bd1cedc16fc556d3d4f0bd73c007f094fbcb86c0007bf920f`
- `onebrain-admin-ui`: `sha256:08a70d2f55ec6c69b256599e45c15c32da7ed4ac64accaf7a4abdc83248f5cde`
- `assistant-service`: `sha256:f21d3cdd56f3294a7ae166a89db865d2c8612c6df6f73129db3f42cfd0f169f7`
- Communication services share `sha256:4aba8db9dc524f278ea93be47476e9afbd22b8bb9870e0a9aaeae82bd1330d10`

No mutable image tag is accepted in the staged override.

## Conversion Sequence

### 1. Evidence and Backups

- Record the server identity, running containers, installed release, migration revision, secret epoch, and current public health.
- Create a fresh PostgreSQL custom-format backup covering the current OneBrain database.
- Copy `/opt/onebrain` deployment assets into a timestamped root-only rollback directory without copying Docker volumes.
- Record checksums for the live and staged Compose, Caddy, release marker, image override, and host-agent files.
- Verify every backup is non-empty before changing the live configuration.

### 2. Render and Stage the Full Topology

- Render with deployment ID `onebrain_development_gate`, its existing account ID, Compose project `onebrain-onebrain-development-gate`, FQDN `onebrain-development-gate.onlyonebrain.com`, all eight modules, and candidate `2026.07.18.311`.
- Preserve the current fleet URL, signing keys, registry allowlist, backup policy, Drive policy, and non-secret operational settings.
- Generate per-service environment files containing secret references only; never read or copy plaintext secrets into the local workspace.
- Set profiles to `onebrain assistant communication` and the local module allowlist to the exact eight-module set.
- Stage generated files under a root-only directory on the existing server and validate them with `docker compose config`.
- Reject the conversion if the rendered service set, image set, project name, FQDN, deployment ID, or module allowlist is not exact.

### 3. Credential Hygiene

- The diagnostic session showed that a live service environment file resolves some credentials on the host. Those values are treated as sensitive and are never logged again.
- During the conversion restart, rotate only the affected application-role and Redis credentials through the encrypted Mission Control bundle, bump the secrets epoch, and let the root-owned bootstrap flow apply it.
- Keep the PostgreSQL owner credential stable so the role-management job can authenticate and rotate application roles safely.
- Do not mark the new epoch applied until Postgres role reconciliation, Redis recreation, and dependent-service startup all succeed.

### 4. Activate and Migrate

- Pull all target images while the old stack remains available.
- Atomically replace the staged host assets and retain their originals in the rollback directory.
- Run `postgres-roles`, OneBrain migration/Drive activation, Assistant migration, and Communication migration.
- Start the eight long-running services plus existing infrastructure under the unchanged Compose project.
- Install the current gate agent, reporter, bootstrap helper, and user-management poller with root-only executable permissions.
- Write the installed-release descriptor only after migration and service validation succeed.

### 5. Verify and Reconcile with Mission Control

- Require all eight target containers to be running with zero restart loops.
- Require local and public health checks to succeed.
- Require the database revision to equal `0034_drive_malware_quarantine`.
- Trigger a gate heartbeat and require Mission Control to observe the exact eight-module set, release `2026.07.18.311`, the newly applied secret epoch, and `user_management_v1=true`.
- Retry candidate `2026.07.18.311` through Mission Control. The signed desired state should be a no-op or idempotent reconciliation of the same immutable target.
- Require promotion to reach `dev_verified` and require no open fleet alerts.
- Submit a new directory-snapshot job and verify that the host leases and completes it. Do not create, disable, reset, or delete an account without separate approval.

## Rollback

Rollback begins automatically if asset validation, migration, service startup, exact module verification, public health, or Mission Control reconciliation fails.

1. Stop only newly introduced application services.
2. Restore the timestamped host configuration and previous image override.
3. Restore the PostgreSQL backup only if a migration changed data and the prior application cannot run safely against the migrated schema.
4. Restart the legacy Core stack under the unchanged Compose project.
5. Verify public health and report the exact failed checkpoint.

The rollback never performs a Hetzner server lifecycle action and never changes the server's network identity.

## Testing and Success Criteria

The conversion is complete only when all of the following are true:

- The existing server ID and IP remain unchanged.
- MC remains healthy on build `315`.
- The dev host reports candidate `311` and migration `0034_drive_malware_quarantine`.
- The exact eight application modules are present and healthy.
- Assistant and Communication migrations complete successfully.
- Secret rotation is applied without exposing values.
- The gate agent includes and runs the user-management poller.
- A directory-snapshot job completes and remains queryable from MC.
- Candidate promotion reaches `dev_verified`.
- No Hetzner server was created, replaced, rebuilt, resized, or deleted.

## Deferred UI Correction

The Users page currently keeps its pending job only in component memory, so navigation discards the visible progress state. After the server-side rollout is healthy, a separate change will add durable job restoration and correct expiry reporting. That UI change is not part of this host-conversion operation.

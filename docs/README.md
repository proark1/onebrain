# OneBrain Documentation

OneBrain is a general, GDPR-conscious platform for organizations. The current
production and customer-deployment model is Hetzner-only.

Working in this repository? Start with [AGENTS.md](../AGENTS.md) — the build,
test, and release rules live there, not in this directory.

## Start here

- [Platform overview](../README.md) — product scope, local development, and
  the current deployment topology.
- [Target architecture](target-architecture.md) — isolation boundaries and
  service roles.
- [Mission Control architecture](mission-control-architecture.md) — the
  private super-admin control plane and its broker boundary.
- [Deployment guide](deployment.md) — dedicated customer deployment and
  operational safety.
- [Mission Control standup](mission-control-standup.md) — configuration and
  access rules for the MC host.
- [Updating Mission Control](mission-control-update-runbook.md) — what merging
  to `main` does automatically, where a person is required, and why an MC
  rollout reports a timeout after succeeding.
- [Broker host bundle](../deploy/broker/README.md) — private Hetzner token
  custody, mutual TLS, and host activation checks.
- [Release promotion](release-promotion-activation.md) — dev-gate validation
  and explicit customer rollout.
- [Production activation and recovery](production-activation-runbook.md) —
  external credential retirement, broker activation, multi-replica, canary,
  restore, and isolation checks.
- [Customer box provisioning runbook](customer-box-provisioning-runbook.md) —
  bootstrap-token and one-time-password expiry windows, and the retry traps.

## Technical contracts

- [Data-layer boundary](onebrain-data-layer-boundary.md)
- [Intake pipeline](onebrain-intake-pipeline.md)
- [Migration discipline](onebrain-migrations.md)
- [Service client](onebrain-service-client.md)
- [KPI Dashboard](kpi-dashboard.md)
- [AI Employees](ai-employees.md)

## Drive operations

- [Backup and restore](drive-backup-restore.md)
- [Malware quarantine operations](drive-malware-operations.md)

## Reference and transition material

- [Hetzner fleet architecture](hetzner-fleet-architecture.md) — deployment
  isolation and rollout constraints.
- [Hetzner migration status](hetzner-migration-sequence.md) — what was
  migrated and what remains retired or pending.

## Archive

[`archive/`](archive/README.md) is the single home for dated specs and
implementation plans. Files there are immutable historical records of what was
decided on a given date — they are **not** current operational instructions, and
they are not kept in sync with the code. Write new dated design records there;
do not create a second dated-document directory.

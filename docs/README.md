# OneBrain Documentation

OneBrain is a general, GDPR-conscious platform for organizations. The current
production and customer-deployment model is Hetzner-only.

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
- [Broker host bundle](../deploy/broker/README.md) — private Hetzner token
  custody, mutual TLS, and host activation checks.
- [Release promotion](release-promotion-activation.md) — dev-gate validation
  and explicit customer rollout.

## Technical contracts

- [Data-layer boundary](onebrain-data-layer-boundary.md)
- [Intake pipeline](onebrain-intake-pipeline.md)
- [Migration discipline](onebrain-migrations.md)
- [Service client](onebrain-service-client.md)
- [Deletion, retention, and tombstones](deletion-tombstone-contract.md)

## Reference and transition material

- [Hetzner fleet architecture](hetzner-fleet-architecture.md) — deployment
  isolation and rollout constraints.
- [Hetzner migration status](hetzner-migration-sequence.md) — what was
  migrated and what remains retired or pending.
- [Historical archive](archive/README.md) — immutable dated specs and plans;
  not current operational instructions.

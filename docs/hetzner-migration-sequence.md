# Hetzner Migration Status

OneBrain's active deployment direction is Hetzner-only. This document records
the remaining transition safeguards; it is not a Railway runbook.

## Completed foundation

- Mission Control is deployed on its own Hetzner host as the private
  super-admin control plane.
- The dev gate is an isolated, full-stack, dummy-data customer-shaped
  environment.
- Customer application rendering denies fleet and operator surfaces and keeps
  fleet credentials out of application containers.
- Release descriptors use immutable image digests and signature verification.
- Shared AI Communication images are configured by their explicit service role.
- Active provisioning and rollout behavior is Hetzner-only; the retired Railway
  provisioning/update workflows are absent from the repository.
- Postgres holds shared authentication-rate-limit counters and control-plane
  state so production API replicas see the same lockout and rollout decisions.
- The remote broker transport and dedicated-host bundle are implemented; they
  remain inactive until their separate host and credentials are verified.

## Required before MC-managed customer creation

1. Revoke the old Railway credentials, GitHub Actions secrets/variables,
   workflow permissions, webhooks, and integrations outside this repository.
2. Deploy the dedicated Hetzner broker with the cloud API token kept on that
   host only; verify mTLS, broker credential, loopback service binding, and
   default-deny firewall access from MC only.
3. Configure the release verification public keys, registry allowlist, MC
   public endpoint, customer DNS zone, desired-state signing keys, and default-
   deny firewall profile. Confirm the production MC preflight passes.
4. Run the current Alembic head before scaling API or worker replicas. Verify
   shared login limiting across at least two API replicas and keep untrusted
   forwarding headers out of client-address selection.
5. Exercise a dummy-data customer creation, update, rollback, isolated
   backup/restore, and tenant-isolation canary.
6. Enable customer creation only after the broker, dev gate, recovery path,
   and monitoring alerts are verified together.

## Retirement rules

- Do not use Railway for production or customer deployments, and do not retain
  a Railway credential or organization-level workflow that can deploy OneBrain.
- Do not move a Hetzner API token onto MC or a customer host to bridge an
  incomplete broker implementation.
- Historical archived documents may mention Railway, but they are not active
  operational instructions. Historical database column names remain a storage
  compatibility detail, not a selectable provider.

## Validation checklist

Before a real customer migration or deployment, confirm:

- each customer has a dedicated data and secret boundary;
- MC sees only sanitized deployment metadata;
- the customer exposes no fleet or operator route;
- release identity is an immutable signed digest set;
- database backup/rollback and restore steps are tested;
- a failed rollout cannot alter another customer deployment;
- worker and AI leases recover only after expiry and cannot be completed by a
  stale owner;
- the configured LiteLLM embedding dimension matches both provider output and
  the migrated pgvector schema; and
- a completed two-person teardown review records `execution_disabled`, not a
  cloud deletion.

See the [production activation runbook](production-activation-runbook.md) for
the evidence to capture for these checks.

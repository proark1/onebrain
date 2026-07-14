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

## Required before MC-managed customer creation

1. Deploy the dedicated Hetzner broker with the cloud API token kept on that
   host only.
2. Configure authenticated MC-to-broker requests, request allowlists, and
   network source restrictions.
3. Configure the release verification public keys, registry allowlist, MC
   public endpoint, customer DNS zone, and default-deny firewall profile.
4. Exercise a dummy-data customer creation and release/rollback canary.
5. Enable customer creation only after the broker, dev gate, and recovery path
   are verified together.

## Retirement rules

- Do not use Railway for new production or customer deployments.
- Do not move a Hetzner API token onto MC or a customer host to bridge an
  incomplete broker implementation.
- Treat remaining Railway-specific code, workflows, and archived documents as
  legacy compatibility material until it is intentionally removed in a separate
  reviewed change.

## Validation checklist

Before a real customer migration or deployment, confirm:

- each customer has a dedicated data and secret boundary;
- MC sees only sanitized deployment metadata;
- the customer exposes no fleet or operator route;
- release identity is an immutable signed digest set;
- database backup/rollback steps are tested; and
- a failed rollout cannot alter another customer deployment.

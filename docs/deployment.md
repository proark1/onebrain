# OneBrain Deployment

This guide describes the active Hetzner deployment model. Railway is not a
production or customer-deployment path.

## Deployment shapes

| Shape | Purpose | Data | Control-plane access |
| --- | --- | --- | --- |
| Mission Control | Global super-admin and release control | Deployment metadata only | Private admin and authenticated machine endpoints |
| Development gate | Full customer-shaped test environment | Dummy data only | Reports sanitized metadata; no MC UI |
| Customer deployment | Dedicated customer workspace | That customer's data only | None |
| Hetzner broker | Bounded infrastructure authority | No customer data | Private requests from MC only |

Each deployment has its own database, application secrets, service keys, and
network boundary. No customer deployment may share a database, service key, or
fleet endpoint with another customer.

## Customer suite

The development gate validates the complete supported suite:

- OneBrain API, worker, admin UI, and Postgres/pgvector;
- AI Communication services, including the shared image's API, widget, voice,
  and worker roles; and
- Personal Assistant services.

The same isolation rules apply to customer stacks. Modules are enabled
deliberately for a customer; a module must not create a route into Mission
Control or another customer deployment.

## Provisioning boundary

Mission Control records intent and deployment state. It never stores the
Hetzner API token and must not call Hetzner directly.

The private Hetzner broker holds that token and accepts a small set of
authenticated, validated operations from MC: create or replace a bounded
server, attach allowed networking and storage, set approved DNS records, and
return sanitized provisioning results. It does not expose a customer-facing UI
or customer data endpoint.

The remote broker transport is an activation dependency. Until it is deployed
and trusted, do not enable MC-managed infrastructure creation or bypass the
boundary by placing a Hetzner token on MC.

## Safe release and rollout

1. Build immutable container images and publish a signed release descriptor.
2. Deploy the candidate to the dummy-data, full-stack development gate.
3. Verify service health, schema compatibility, and the product flow.
4. In Mission Control, record the approved release and select an individual
   customer deliberately.
5. Create a recoverable rollout: preserve the prior image digests and database
   backup/restore point before a schema-changing change.
6. Verify the customer's health and version report; mark the rollout complete
   only after checks pass.

Any failed verification leaves the previous customer release in place or
returns it to the preserved known-good release. Do not auto-advance a customer
because a newer dev build exists.

## Required production safeguards

- Use immutable image digests, never mutable tags as a release identity.
- Enforce TLS, strong per-deployment secrets, and Postgres RLS.
- Limit SSH to deliberate break-glass access.
- Restrict MC administration through VPN or an IP allowlist.
- Deny fleet, operator, provisioning, and rollout routes on every customer
  ingress.
- Keep customer application containers free of Hetzner credentials and MC
  control-plane keys.

See [release promotion](release-promotion-activation.md) for the release
workflow and [target architecture](target-architecture.md) for the isolation
model.

# OneBrain Deployment

This guide describes the active Hetzner deployment model. Railway is not a
production or customer-deployment path, and no active provisioning or rollout
workflow may use it. Retiring old Railway credentials and organization-level
automation is an external operator task in the
[production activation runbook](production-activation-runbook.md).

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

The remote broker implementation and host bundle are available. It remains an
activation dependency until a dedicated host, mTLS trust, broker credential,
firewall restriction, and token custody have been verified. Until then, do not
enable MC-managed infrastructure creation or bypass the boundary by placing a
Hetzner token on MC.

## Multi-replica runtime boundary

Production API replicas share PostgreSQL-backed authentication-rate-limit state.
Each failed login increments atomic account and client-address counters whose
subjects are stored only as keyed hashes. A replica uses the direct peer address
unless an explicitly configured, trusted proxy CIDR and hop count permits a
forwarded address. Never trust a caller-supplied forwarding header by default.

Run migrations before scaling API or worker replicas. Background jobs and
direct AI Employee turns carry random lease tokens and expiries; heartbeats and
terminal writes are conditional on the current token. A stopped replica may
leave work for recovery after expiry, but a stale owner cannot complete or fail
work claimed by another replica. Handlers must make their external effects
idempotent by job ID before lease recovery is enabled.

For a LiteLLM + pgvector production deployment, the configured embedding
dimension is part of the persisted vector contract. Startup preflight verifies
provider reachability/output dimension and pgvector schema dimension before
traffic is served. Changing a model or dimension requires a deliberate
versioned re-embedding migration; it is never a runtime inference or table
rebuild.

## Safe release and rollout

1. Build immutable container images and publish a signed release descriptor.
2. Deploy the candidate to the dummy-data, full-stack development gate.
3. Verify service health, schema compatibility, exact rollout
   attempt/release/migration/module reports, and the product flow.
4. In Mission Control, record the approved release and select an individual
   customer deliberately.
5. Create a recoverable rollout: preserve the prior image digests and database
   backup/restore point before a schema-changing change.
6. Verify the customer's health and version report; mark the rollout complete
   only after checks pass.

Any failed verification leaves the previous customer release in place or
returns it to the preserved known-good release. Do not auto-advance a customer
because a newer dev build exists.

Before allowing real customer creation, perform a broker-provisioned dummy-data
canary, an explicit update and rollback canary, an isolated backup/restore
rehearsal, and tenant-isolation negative tests. A stale, malformed, or
mismatched customer report remains pending until its deadline and then fails;
it must not complete a rollout.

## Teardown review boundary

Customer teardown is intentionally non-destructive. The operator flow can bind
a review request to a deployment/account, capture legal-hold and
backup/retention evidence references, and record two distinct approvals. It
stores only a hash of the short-lived approval nonce and blocks active legal
holds. Its terminal state is explicitly `execution_disabled`; no broker or
cloud deletion action is invoked. A future live deletion capability needs a
separate reviewed design and external legal/retention authorization.

## Required production safeguards

- Use immutable image digests, never mutable tags as a release identity.
- Enforce TLS, strong per-deployment secrets, and Postgres RLS.
- Limit SSH to deliberate break-glass access.
- Restrict MC administration through VPN or an IP allowlist.
- Deny fleet, operator, provisioning, and rollout routes on every customer
  ingress.
- Keep customer application containers free of Hetzner credentials and MC
  control-plane keys.
- Alert on broker health, rollout reconciliation deadlines, shared login-limit
  spikes, lease loss/retry exhaustion, backup freshness, and embedding
  preflight failures.

See [release promotion](release-promotion-activation.md) for the release
workflow, [target architecture](target-architecture.md) for the isolation
model, and the [production activation runbook](production-activation-runbook.md)
for the operator-owned activation and recovery steps.

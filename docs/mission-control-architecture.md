# Mission Control Architecture

Mission Control (MC) is OneBrain's private global super-admin control plane. It
answers: which isolated deployment exists, which signed release it runs, when
it was deployed, whether it is healthy, and whether a human explicitly
authorized an update.

It is not a customer application, a shared tenant, or a customer-data
aggregation system.

MC may run on more than one API replica. Its durable control-plane,
authentication-rate-limit, session, rollout, and audit state must therefore be
in the shared PostgreSQL deployment rather than in a process-local cache.

## Roles and trust boundaries

```text
Global super-admin
        |
        v
Mission Control ── authenticated bounded requests ──> Hetzner broker
        ^                                                   |
        | sanitized health/version reports                  | Hetzner API token
        |                                                   v
Dev gate and dedicated customer deployments          Hetzner infrastructure
```

### Mission Control

- Holds deployment metadata, release metadata, health/version observations,
  rollout history, record-only teardown reviews, and MC-specific signing
  material.
- Exposes a protected administrator interface and authenticated
  machine-to-machine endpoints for metadata reports and desired state.
- Does not hold the Hetzner API token.
- Does not read customer database rows, documents, prompts, conversations,
  support logs, or application secrets.

### Hetzner broker

- Runs as a separate private service with no customer UI and no customer data.
- Holds the Hetzner API token in its root-owned environment only.
- Authenticates MC and validates every request against an allowlist of locations,
  server sizes, network/firewall profiles, DNS zones, labels, and resource
  limits.
- Returns sanitized infrastructure identifiers and failure causes; it never
  returns the token or broad cloud inventory to MC.

### Dev gate and customer deployments

- The dev gate is a full customer suite with dummy data. It receives no fleet
  UI, customer-data access, or cloud credentials.
- Each customer deployment has an independent data boundary and can report only
  its own sanitized version/health state.
- Customer ingress denies fleet, operator, provisioning, and rollout routes.

## Rollout authority

MC records an approved release only after it is signature-verified and tested
on the full-stack dev gate. A super-admin then selects an individual customer
for rollout. The broker may prepare infrastructure; the customer host applies a
verified desired state and reports the result. A later release never changes a
customer merely because it exists.

The reconcile path accepts completion only from the current rollout attempt
whose release, migration, enabled-module versions, and health all match the
expected state. This makes an old or malformed report a pending/failing
condition, not an implicit success.

## Safety properties

- Immutable image digests and signed release descriptors identify releases.
- Database/schema changes require a compatible release and a recoverable
  backup/rollback path.
- MC UI access is restricted separately from its necessary public
  machine-to-machine endpoint.
- Metadata errors are sanitized; content-bearing logs do not enter MC.
- Loss of broker connectivity blocks provisioning and does not weaken the
  credential boundary.
- API replicas share hashed fixed-window login counters in PostgreSQL. Client
  forwarding headers are accepted only through an explicit trusted-proxy
  boundary.
- Jobs and direct AI turns use token-fenced leases. A lease can recover after a
  process failure, while a stale owner cannot write a terminal outcome.
- A production LiteLLM + pgvector deployment checks its configured embedding
  dimension against the provider and schema before serving traffic.

## Teardown boundary

MC can record a customer teardown review but cannot execute a deletion. The
review requires the bound customer/account, legal-hold and backup/retention
evidence, a short-lived nonce stored only as a hash, and two distinct
approvers who are not the requester. Its terminal result is explicitly
`execution_disabled` and is audited. No record grants broker or cloud-delete
authority.

## Implementation status

The MC host and isolated dev-gate boundaries are in place. The remote broker
transport and host bundle are implemented but not yet activated with a
dedicated host and secrets; automated MC provisioning remains disabled until
that verification is complete. See the
[broker deployment bundle](../deploy/broker/README.md) and the historical
[broker design](archive/specs/2026-07-15-hetzner-broker-isolation-design.md).
The active
[production activation runbook](production-activation-runbook.md) names the
external credential, broker, canary, restore, and isolation evidence needed to
enable it.

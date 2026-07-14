# Mission Control Architecture

Mission Control (MC) is OneBrain's private global super-admin control plane. It
answers: which isolated deployment exists, which signed release it runs, when
it was deployed, whether it is healthy, and whether a human explicitly
authorized an update.

It is not a customer application, a shared tenant, or a customer-data
aggregation system.

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
  rollout history, and MC-specific signing material.
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

## Safety properties

- Immutable image digests and signed release descriptors identify releases.
- Database/schema changes require a compatible release and a recoverable
  backup/rollback path.
- MC UI access is restricted separately from its necessary public
  machine-to-machine endpoint.
- Metadata errors are sanitized; content-bearing logs do not enter MC.
- Loss of broker connectivity blocks provisioning and does not weaken the
  credential boundary.

## Implementation status

The MC host and isolated dev-gate boundaries are in place. The remote broker
transport remains a separate activation task; until it is complete, automated
MC provisioning remains disabled. See the historical
[broker design](archive/specs/2026-07-15-hetzner-broker-isolation-design.md)
for the approved implementation record.

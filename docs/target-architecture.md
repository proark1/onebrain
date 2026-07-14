# OneBrain Target Architecture

OneBrain is a general platform deployed as isolated customer environments on
Hetzner. The isolation unit is one customer deployment, not a shared product
tenant or a project-specific installation.

## Customer environment

Every customer environment has its own:

- OneBrain API, worker, administration interface, and Postgres/pgvector data;
- optional AI Communication and Personal Assistant services selected for that
  customer;
- secrets, service keys, backups, network policies, and host-level storage;
- public hostname and ingress policy.

The development gate uses the same full suite with dummy data so that it is a
realistic release target without becoming a control-plane or fleet console.

## Isolation requirements

- Requests are scoped by authenticated account/workspace identities and
  enforced by database RLS in non-local environments.
- No customer application receives a fleet URL, fleet key, operator mode,
  provisioner credential, or Hetzner credential.
- Customer ingress returns no fleet, operator, provisioning, or rollout API
  surface.
- Shared images are permitted; shared customer databases, secrets, and
  runtime queues are not.
- A customer deployment reports only its own sanitized health, release digest,
  and agent status to MC.

## Platform services

| Service | Responsibility | Must not do |
| --- | --- | --- |
| OneBrain | Scoped knowledge, governance, retrieval, and application APIs | Cross customer boundaries |
| AI Communication | Customer communication workflows and UI | Receive fleet credentials or foreign data |
| Personal Assistant | Customer assistant workflows | Become a control plane |
| Dev gate | Test a complete candidate with dummy data | Access other customer data or MC UI |
| Mission Control | Super-admin deployment metadata and explicit rollout decisions | Handle customer content or cloud credentials |
| Hetzner broker | Bounded cloud actions for MC | Expose customer UI/data or return cloud credentials |

## Release lifecycle

```text
immutable signed candidate
          |
          v
full-stack dev gate with dummy data
          |
          v
super-admin approval in Mission Control
          |
          v
one explicitly selected customer deployment
          |
          v
health/version verification or rollback
```

This lifecycle is intentionally manual at the customer-selection step. A
successful dev-gate test makes a release eligible; it does not make it deployed
to every customer.

## Data boundary

MC receives only deployment metadata. Customer content remains in the
customer's environment. The broker holds no customer application data. See the
[data-layer boundary](onebrain-data-layer-boundary.md) and
[deletion contract](deletion-tombstone-contract.md) for data-level rules.

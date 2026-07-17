# Hetzner Deployment and Fleet Architecture

This document defines the Hetzner-only boundary for OneBrain deployments.

## Principle

Mission Control decides *what* may be created or updated. The Hetzner broker
performs the bounded infrastructure action. A dev gate or customer server
applies its own verified release and reports a sanitized result.

No single component should both hold broad cloud credentials and process
customer content.

## Broker operation contract

MC may request only typed, validated operations such as:

- create a server from an approved image/size/location profile;
- attach an allowed network, firewall, volume, and DNS record;
- retrieve the status of a deployment MC already owns; and
- create or idempotently reuse a bounded deployment requested by MC.

The broker rejects unknown locations, oversized servers, unapproved domains,
cross-customer identifiers, unbounded resource counts, and requests missing MC
authentication. It exposes no customer UI and no general-purpose Hetzner proxy.

## Destructive lifecycle

The active broker does not expose a destroy operation. Customer teardown review
is a record-only MC workflow: it binds a request to the customer/account,
requires legal-hold and backup/retention evidence, and records two independent
approvals. Its terminal result is `execution_disabled`; it does not call the
broker or Hetzner. A future deletion executor requires an independently
reviewed design and external legal/retention authorization.

## Host responsibilities

| Host | Credentials | Reports | Prohibited data/access |
| --- | --- | --- | --- |
| MC | MC auth, release verification public keys, desired-state signing key | Deployment metadata | Hetzner token, customer content |
| Broker | Hetzner API token, MC trust material | Sanitized cloud result | Customer databases, UI, app secrets |
| Dev gate | Its own app secrets and dev verification public key | Its own health/version | MC UI, fleet control, cloud credentials |
| Customer server | Its own app secrets and production verification public key | Its own health/version | Other customer data, MC UI, cloud credentials |

## Network policy

- The broker accepts requests only from MC through an authenticated private
  channel, mTLS client certificate verification, broker credential, and source
  restriction.
- The broker service listens only on loopback; its TLS front end is the only
  ingress. The Hetzner firewall is default-deny and permits HTTPS only from the
  documented MC egress address or addresses. SSH remains a separately reviewed
  break-glass path.
- MC administration is limited through VPN or IP allowlisting; its public
  machine endpoint accepts only the required authenticated protocol.
- Customer ingress exposes customer functions only. It explicitly denies fleet,
  operator, provisioning, and rollout routes.
- SSH is break-glass only and excluded from the normal public firewall policy.

## Release safety

The fleet does not pull mutable image tags as release identities. Each desired
state names a signed release and immutable image digests. The host verifies the
signature before applying changes. Rollback means applying the previously
recorded verified state, not guessing a tag.

A rollout completes only after a report for its current attempt identifies the
expected release, migration, enabled-module versions, and healthy application
state. A stale, malformed, or mismatched report stays pending until its
convergence deadline and then fails explicitly.

## Current activation state

The remote broker transport and host bundle are implemented. They must be
deployed and verified with a dedicated host, mTLS, source-restricted firewall,
and broker-only token before MC provisions customer servers. Until then, MC
must not gain a direct Hetzner token as a shortcut. Use the
[production activation runbook](production-activation-runbook.md) for the
required firewall, mTLS, canary, rollback, restore, and isolation proof.

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
- destroy or replace a deployment only through an explicit, auditable request.

The broker rejects unknown locations, oversized servers, unapproved domains,
cross-customer identifiers, unbounded resource counts, and requests missing MC
authentication. It exposes no customer UI and no general-purpose Hetzner proxy.

## Host responsibilities

| Host | Credentials | Reports | Prohibited data/access |
| --- | --- | --- | --- |
| MC | MC auth, release verification public keys, desired-state signing key | Deployment metadata | Hetzner token, customer content |
| Broker | Hetzner API token, MC trust material | Sanitized cloud result | Customer databases, UI, app secrets |
| Dev gate | Its own app secrets and dev verification public key | Its own health/version | MC UI, fleet control, cloud credentials |
| Customer server | Its own app secrets and production verification public key | Its own health/version | Other customer data, MC UI, cloud credentials |

## Network policy

- The broker accepts requests only from MC through an authenticated private
  channel and source restriction.
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

## Current activation state

The remote broker transport and host bundle are implemented. They must be
deployed and verified with a dedicated host, mTLS, source-restricted firewall,
and broker-only token before MC provisions customer servers. Until then, MC
must not gain a direct Hetzner token as a shortcut.

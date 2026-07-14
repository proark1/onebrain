# Full-Stack Development Gate Tenant Isolation

**Status:** Approved design — awaiting final specification review
**Date:** 2026-07-13

## Purpose

The development release gate must be a disposable but realistic customer tenant.
It runs the complete customer suite — OneBrain, Personal Assistant, and AI
Communications — and receives releases before customers do. Tenant users must
only see and operate their own products and data. They must not see, invoke, or
possess credentials for Mission Control, fleet management, provisioning,
rollouts, or any other deployment.

The development gate remains the automated release-verification target. That
requires a minimal one-way control-plane channel, but this channel belongs to a
host-only agent rather than to customer-facing containers.

## Goals

1. Rebuild the disposable development gate as the existing `full_stack` bundle.
2. Keep OneBrain, Assistant, and AI Communications data and credentials scoped
   to the development tenant only.
3. Keep all fleet credentials and signed-release processing outside user-facing
   containers.
4. Preserve automatic development release verification without exposing a fleet
   interface to tenant users.
5. Reject control-plane URLs at the public edge and keep internal infrastructure
   ports private.
6. Retain the current server size initially and make its resource headroom an
   acceptance check.

## Non-goals

- Preserving the current development gate's dummy data or its existing host.
- Giving the development tenant visibility into Mission Control or other tenant
  deployments.
- Enabling customer rollout automation as part of this work.
- Connecting real production communication providers or sending real customer
  messages during bootstrap.
- Changing production release-key custody or putting the production private key
  on a server.

## Target topology

```text
Internet
  -> HTTPS Caddy
       -> OneBrain customer UI/API
       -> Personal Assistant
       -> AI Communications API, widget, voice, workers

Host-only onebrain-gate-agent
  -> authenticated outbound desired-state fetch from Mission Control
  -> local verified update/recovery flow and health probes
  -> authenticated outbound sanitized heartbeat to Mission Control
```

The customer services run as the existing `full_stack` compose bundle. Their
separate local databases are `onebrain`, `assistant`, and `communication`; Redis
is local to the stack. No service mounts the gate-agent configuration or reads a
fleet credential.

## Access boundaries

### Customer stack

- `ONEBRAIN_OPERATOR_MODE=false` and `ONEBRAIN_OPERATOR_CONSOLE=false` are
  explicit customer-stack invariants.
- OneBrain, Assistant, and Communications containers receive only tenant-local
  database, Redis, service, and account/space credentials.
- They receive neither `ONEBRAIN_FLEET_KEY`, `ONEBRAIN_FLEET_URL`, desired-state
  signing material, a provisioning callback credential, nor a Mission Control
  administrator credential.
- The application therefore does not mount operator, provisioning, rollout, or
  fleet routers. The browser UI also renders the normal customer surface only.
- Caddy explicitly rejects fleet, operator, provisioning, and rollout path
  prefixes before the generic API proxy. This makes the public boundary clear
  even if an application route is accidentally added later.

### Host-only gate agent

- A root-owned systemd service, `onebrain-gate-agent`, replaces in-app release
  reporting for the development gate.
- Its configuration is root-readable only and is not bind-mounted into Compose.
- Its deployment credential is pinned by Mission Control to exactly this
  development deployment. It can fetch only this deployment's desired state and
  submit only this deployment's heartbeat.
- It verifies signed desired state before invoking the existing guarded update
  and recovery mechanism. It reports only version, migration, enabled-module,
  update-attempt, health, and resource-status metadata.
- It never reads, sends, or indexes customer documents, conversations, messages,
  users, or raw provider credentials.

### Network and infrastructure

- Hetzner firewall permits public TCP 80 and 443 only. SSH remains closed except
  for a time-bounded, source-IP-limited break-glass operation.
- Postgres, Redis, agent, and Docker control sockets are not public.
- The customer stack cannot access Mission Control's database, filesystem,
  Docker socket, or operator credentials.

## Full-stack baseline and provisioning

The replacement is provisioned from a complete, production-signed baseline
manifest. It must provide digest-pinned image references and module versions for
all `full_stack` modules:

```text
onebrain-api
onebrain-admin-ui
onebrain-workers
assistant-service
communication-api
communication-widget
communication-voice
communication-workers
```

An incomplete manifest, tag-only reference, invalid signature, or missing
module-specific integration configuration blocks provisioning. There is no
fallback image or permissive partial bundle.

Bootstrap creates a fresh development account, the standard full-stack spaces
and applications, a development-only administrator, per-deployment integration
keys, and synthetic content. Assistant and communications integrations start in
test-safe mode; no real customer provider account or outbound production
messaging is activated by bootstrap.

## Replacement and cutover

1. Leave automatic candidate registration, fleet reconciliation, and customer
   rollout activation disabled.
2. Validate the full-stack signed baseline and render a replacement box with the
   host-only gate agent.
3. Provision the replacement alongside the current disposable gate with a
   distinct temporary deployment ID and hostname, so Hetzner's idempotency labels
   and DNS records cannot collide.
4. Require bootstrap, module smoke tests, route-denial tests, credential
   inspection, and a fresh sanitized heartbeat to pass.
5. Designate the replacement as the sole development gate in Mission Control.
6. Delete the old disposable server, its volume, firewall, DNS record, and its
   revoked deployment credentials only after designation is durable.

If replacement verification fails, the current gate stays designated and no
customer state changes. Because the existing data is dummy data, no restore or
data migration is required for the replacement path.

## Failure behavior

- A desired-state fetch, signature, update, or smoke failure holds the last
  known-good development stack and marks the gate stale or unhealthy.
- An unhealthy or stale gate blocks candidate verification and therefore blocks
  customer approval. It never triggers a customer rollout.
- A host-agent failure does not make fleet controls available to tenant users.
- If the full suite exceeds current CPU or memory capacity, the gate fails its
  resource acceptance check and is resized before release automation is enabled.

## Acceptance tests

The implementation is accepted only when all of the following are true:

1. A tenant administrator can sign in and use OneBrain, Personal Assistant, and
   AI Communications with fresh dummy data.
2. Every enabled module and each of the three local databases passes health and
   migration checks.
3. Customer containers contain no fleet credential, desired-state private key,
   operator-mode setting, or Mission Control administrator credential.
4. Public requests to fleet, operator, provisioning, and rollout paths are
   rejected; the corresponding application routers are absent.
5. The host-only agent can fetch only this deployment's signed desired state and
   submit only its own metadata-only heartbeat.
6. The agent heartbeat is fresh, healthy, version/migration/module-consistent,
   and sufficient for Mission Control to designate the replacement gate.
7. Internet scans confirm that only web entry ports are public (HTTP redirect and
   HTTPS); database, cache, SSH, and agent control ports are not reachable.
8. CPU and memory observations under full-stack smoke load leave acceptable
   headroom on the current server size, or the gate is explicitly resized before
   it is used for automated verification.
9. No candidate registration, reconciliation loop, production-signature upload,
   customer approval, or customer rollout is enabled by this work.

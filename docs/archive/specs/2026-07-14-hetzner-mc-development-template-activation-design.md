# Hetzner Mission Control and development-template activation

Date: 2026-07-14

## Purpose

Operate OneBrain entirely on dedicated Hetzner servers. `mc.onlyonebrain.com`
is the sole Mission Control (MC) server. A second dedicated Hetzner server is a
customer-shaped, disposable development environment containing OneBrain, AI
Communication, and Personal Assistant. It validates releases before an operator
chooses to deploy them to real customer servers.

The existing MC host is `onebrain-mc` at `mc.onlyonebrain.com`; it is already in
operator mode. It is upgraded in place so its control-plane database and
administrator identity remain intact. Railway is not part of this architecture.

## Topology and trust boundaries

```text
Operator browser
    -> https://mc.onlyonebrain.com (Mission Control, dedicated Hetzner host)
    -> Hetzner Cloud API (create and track dedicated customer hosts)

Development customer host
    -> customer-facing OneBrain / AI Communication / Personal Assistant
    -> root-only update and heartbeat agent
    -> authenticated, metadata-only outbound requests to Mission Control
```

Mission Control alone has the operator console, fleet registry, release records,
provisioning routes, and Hetzner provider credential. The development host is a
normal customer data plane: its containers must not receive fleet credentials,
release-signing material, provider credentials, provisioning callback authority,
or other-customer metadata. Its Caddy configuration must return `404` for all
operator, fleet, provisioning, rollout, and cross-customer paths.

The root-owned host agent is the only component on the development box that can
read its deployment credential. It submits a sanitized `fleet.v2` heartbeat
containing version, migration, declared modules, and health only. It never sends
customer content, URLs, logs, credentials, or free-text diagnostic data.

## Release baseline

The development template is the `full_stack` bundle and must contain exactly
these eight release modules:

- `onebrain-api`, `onebrain-admin-ui`, `onebrain-workers`
- `assistant-service`
- `communication-api`, `communication-widget`, `communication-voice`,
  `communication-workers`

Every release image is immutable and digest-pinned. The current communication
repository deliberately publishes one shared image; the rendered environment
must set its `SERVICE` selector to `api`, `widget`, `voice`, or `workers` for the
corresponding container. The same verified communication digest may therefore
appear under each of those four module IDs. The assistant image is separately
digest-pinned.

Mission Control may create the development server only from an active release
whose image map covers all eight modules, passes its local registry allowlist,
and has a valid offline production signature. The production private key stays
offline and is never copied to MC, the development host, CI, or an application
container. A development key may register candidates, but cannot substitute for
the production signature needed for the initial baseline.

## Activation sequence

1. Upgrade the existing `onebrain-mc` host to the current pinned MC image and
   run its database migration before serving the new control-plane functions.
2. Configure only MC with its public URL, Hetzner API token, registry allowlist,
   production release-verification public key, desired-state signer, and related
   non-secret control-plane settings. Retain the release-promotion enforcement
   flag in report-only mode until the gate has proven healthy.
3. Correct the full-stack renderer to set the communication `SERVICE` values;
   test the rendered compose and customer route isolation.
4. Publish current immutable images, resolve their registry digests, and prepare
   the complete release manifest. Sign it offline, register it in MC, and make
   it active only after signature and digest validation succeed.
5. Call the development-gate endpoint with `dry_run=true`, review its fixed
   customer account, bundle, region, and release values, then repeat with
   `dry_run=false`. Provisioning creates a separate Hetzner server and fresh
   dummy data only.
6. Confirm TLS, all eight module health probes, root-agent heartbeats, migration
   revision, one-time bootstrap, customer route denials, and no customer-visible
   fleet configuration. Designate the deployment as the release gate only after
   MC reports no blockers.

## Failure handling

- Missing provider configuration, incomplete images, unsigned manifests, or an
  invalid signature fail before Hetzner resources are created.
- A failed cloud-init, migration, or health probe leaves the new server
  undesignated; the existing gate is not replaced.
- The root reporter is non-fatal to local app availability but causes MC to
  reject gate designation when its heartbeat becomes stale or unhealthy.
- Customer releases remain operator-selected and one-at-a-time. Development
  verification does not automatically deploy a release to any customer.

## Verification

Before activation, run the Python test suite, compile checks, rendered-compose
validation, and Caddy adaptation checks. During activation, record the dry-run
result, release manifest digest, provider run identifier, development-host
hostname, health results, and MC gate blocker result. No credential or customer
data is placed in those records.

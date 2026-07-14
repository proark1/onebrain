# OneBrain deployment context

## Authoritative production topology

OneBrain is deployed on dedicated Hetzner servers. Railway is not part of the
production, development-gate, or customer deployment architecture.

```text
Super admin
  -> Mission Control (MC): mc.onlyonebrain.com
  -> Infrastructure broker: private Hetzner service
  -> Development gate: isolated full customer suite
  -> Customer servers: isolated full customer suites
```

### Mission Control

- `mc.onlyonebrain.com` is the global super-admin control plane, not a customer
  application and not a customer data store.
- Only the super admin uses its dashboard. Restrict the administrator UI with a
  VPN or an IP allowlist whenever operationally possible.
- MC still receives authenticated machine-to-machine traffic from enrolled
  development and customer hosts: sanitized heartbeats, desired-state fetches,
  bootstrap exchanges, and provisioning callbacks.
- MC owns customer/deployment metadata, release manifests, approval state,
  fleet health, and the decision to deploy a release. It never automatically
  deploys a verified release to customers.
- Never expose MC routes, credentials, or cross-customer metadata through a
  customer-facing deployment.

### Development gate

- The development gate is a dedicated Hetzner customer-shaped environment with
  disposable dummy data.
- It runs the full customer suite: OneBrain, AI Communication, and Personal
  Assistant.
- It can access only its own OneBrain account, spaces, service keys, and data.
  It has no fleet UI, provisioning UI, rollout UI, MC administrator credentials,
  provider credential, release private key, or visibility into another customer.
- Its application containers deny `/api/fleet`, `/api/operator`,
  `/api/provisioning`, and `/api/rollouts`. A root-only host agent sends a
  metadata-only outbound release heartbeat to MC.
- A development release must be explicitly verified and approved before an
  operator selects any customer deployment. Verification never auto-rolls out
  to customers.

### Infrastructure broker

- The broker is a small dedicated Hetzner service with no user interface,
  customer data, or customer product modules.
- It holds the Hetzner API token so the token is never placed in Mission
  Control, development servers, or customer servers.
- MC requests bounded provisioning operations through an authenticated private
  broker API using mutual TLS and a broker-scoped credential. The broker
  implementation lives in `app/provisioning/hetzner/remote.py` and
  `app/provisioning/hetzner/broker_service.py`; its host bundle is in
  `deploy/broker/`.
- The broker enforces the approved EU regions, instance sizes,
  images, network/firewall shape, DNS zone, fleet labels, and server-count cap.
- The broker exposes no automatic destructive operation. Do not reintroduce an
  in-process MC broker or loosen the production broker guard merely to make
  provisioning easier.
- Activating the broker still requires a separate Hetzner host, source-restricted
  firewall, mTLS certificates, a broker credential, and the Hetzner API token
  installed only in the broker's root-owned environment.

## Release and secret rules

- Every deployable image is digest-pinned. Do not substitute floating tags.
- The production release private key remains offline. MC verifies signatures
  using only its public key; a development signing key cannot approve customer
  releases.
- The development/customer root agent may hold a deployment credential only.
  It must not pass that credential into application containers.
- Do not print, commit, copy into rendered artifacts, or expose in browser/API
  responses any API token, private key, client credential, bootstrap password,
  service-key plaintext, or customer content.

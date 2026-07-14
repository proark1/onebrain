# Dedicated Hetzner broker for Mission Control

Date: 2026-07-15

## Purpose

Mission Control at `mc.onlyonebrain.com` is the super-admin control plane. It
needs to create dedicated Hetzner development and customer servers, but it must
not hold the Hetzner API token. This design adds a small dedicated broker
service that is the only process permitted to hold that token.

The broker is neither a customer deployment nor a user-facing product. It has
no OneBrain tenant database, no document storage, no LLM access, and no
operator or customer browser UI.

## Chosen topology

```text
Super admin -> Mission Control (admin dashboard)
                     |
                     | mutually authenticated provisioning request
                     v
              Hetzner broker (private service, token custody)
                     |
                     | Hetzner Cloud API
                     v
         Development/customer dedicated Hetzner servers
```

MC remains reachable for authenticated fleet machine traffic, while its human
administrator UI is restricted by VPN or IP allowlist. The broker permits
inbound HTTPS only from the fixed MC host address and authenticates MC with a
broker-scoped credential and TLS client certificate. It must not provide a
browser-accessible administration surface.

## Broker contract

The application gains a real remote `HetznerBroker` implementation and a small
broker HTTP service. MC serializes the existing typed server, volume, firewall,
and DNS request shapes; the broker revalidates every field before invoking the
Hetzner client. The broker returns only provider metadata needed by MC:
server ID, public IPv4 address, volume IDs, DNS record ID, FQDN, reuse state,
and backup-request state.

The broker accepts only a create-or-idempotently-reuse operation. It rejects
all other provider actions and does not implement destroy. It retains the
existing cost cap and enforces all of these server-side:

- Hetzner EU locations only (`nbg1`, `fsn1`, `hel1`).
- The configured Ubuntu image and approved server-size allowlist.
- A default-deny firewall shape; no SSH unless explicitly allowed in broker
  configuration.
- A bounded data-volume size and the `managed-by=onebrain-fleet` label.
- DNS changes only in the configured fleet zone.
- A server name and deployment identifier restricted to the existing inert ID
  grammar.

The broker never accepts an arbitrary raw provider request, token, provider
URL, callback URL, registry allowlist, release key, or free-form shell input
from MC.

## Credential custody

The Hetzner API token exists only in the broker host's root-owned environment.
MC stores a broker URL, a broker-scoped client credential, and its client
certificate/key; it never receives the Hetzner token. The broker stores the MC
client-certificate trust anchor and does not trust a request because its source
IP matches alone.

The release production private key remains offline. The broker does not see it,
nor any deployment service key, bootstrap password, tenant data, or LLM key.

## Deployment

The broker runs as a dedicated, minimal Hetzner host. Its host firewall permits
only TCP 443 from MC's fixed public IPv4 address; all other inbound traffic,
including SSH, is denied. A Caddy reverse proxy terminates TLS and requires a
valid MC client certificate before proxying to the local broker process. The
broker process itself binds only to loopback.

Bootstrap needs one human-provided Hetzner API token to create the initial
broker host and to install that token there. The token is never emitted in
console output, stored in Git, sent to MC, or placed in cloud-init diagnostics.
After bootstrap, MC calls the broker over its authenticated private interface.

## Failure behavior and verification

- If broker authentication, TLS client validation, input validation, or the
  provider call fails, MC records the provisioning run as failed before a
  customer server is marked active.
- A missing/unhealthy broker or unavailable token blocks provisioning; MC,
  existing customer boxes, and the development gate remain running.
- The broker client has short timeouts and treats malformed responses as
  failures.
- Tests cover request/response serialization, authentication rejection,
  input allowlists, idempotent reuse, cost-cap behavior, and the guarantee that
  neither token nor credentials appear in logs or responses.
- Deployment verification proves the broker firewall exposure, an unauthorized
  request rejection, an authenticated dry run, and MC's inability to read the
  Hetzner token.

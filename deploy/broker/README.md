# Dedicated Hetzner Broker Host

This bundle runs the only process that holds the Hetzner Cloud API token. It is
not Mission Control, a customer deployment, or an operator UI. It has no
database, customer data, OneBrain application secrets, or browser routes.

## Before activation

1. Complete the retired-Railway credential/workflow revocation in the
   [production activation runbook](../../docs/production-activation-runbook.md).
   This broker is the only active infrastructure path; do not leave a legacy
   provider principal capable of changing OneBrain deployments.
2. Create a dedicated Hetzner host outside every customer network.
3. Install Docker Compose and Caddy on that host.
4. Restrict the Hetzner Cloud Firewall to TCP 443 from Mission Control's fixed
   egress address or addresses and deny all other ingress by default. Do not
   open SSH except through a separate, reviewed break-glass path.
5. Copy `broker.env.example` to `broker.env`, make it root-owned `0600`, and
   set the Hetzner token there. The raw MC broker credential is not stored on
   the broker; store only its SHA-256 hash.
6. Install `Caddyfile` as the host Caddy configuration. Create
   `/etc/caddy/broker-tls/` with root-owned `server.crt`, `server.key`, and the
   MC client-certificate CA `mc-client-ca.crt`.
   Issue a unique MC client certificate from that CA; do not reuse the broker
   server certificate as a client credential.
7. Set `HETZNER_BROKER_HOST` in Caddy's environment, validate the Caddyfile,
   then restart the Caddy service. The bundled configuration disables Caddy's
   admin API, so `caddy reload` is intentionally unavailable.
8. Set `ONEBRAIN_BROKER_IMAGE` to an immutable image digest that includes this
   broker code, then start the service:

   ```sh
   docker compose --env-file broker.env up -d
   ```

The container publishes only `127.0.0.1:8181`; Caddy is the sole network
ingress and requires a valid MC client certificate for every route.
The Caddyfile also enforces an SNI/Host match to prevent a protected host from
being reached through an unprotected TLS name.

Keep the host management plane distinct from the broker ingress. A firewall
change, mTLS CA/client-certificate rotation, or broker-token rotation requires
the same negative tests as initial activation before provisioning resumes.

## Mission Control configuration

MC receives only these broker values:

```text
ONEBRAIN_HETZNER_BROKER_URL=https://<broker-host>
ONEBRAIN_HETZNER_BROKER_CREDENTIAL=<MC-only-raw-credential>
ONEBRAIN_HETZNER_BROKER_CLIENT_CERTIFICATE_FILE=/root/broker-tls/mc-client.crt
ONEBRAIN_HETZNER_BROKER_CLIENT_KEY_FILE=/root/broker-tls/mc-client.key
ONEBRAIN_HETZNER_BROKER_CA_FILE=/root/broker-tls/broker-ca.crt
ONEBRAIN_HETZNER_API_TOKEN=
```

The final line is intentional: MC must not hold the Hetzner token. The broker
rejects a request that fails its Caddy mTLS check, its MC credential check, or
its allowlists. It exposes `/health`, `/v1/provision`, and `/v1/destroy` — the last a
guarded, discovery-scoped teardown that, given a deployment id, deletes only that
deployment's own labelled server/volume/firewall/DNS resources (it cannot be handed a
foreign resource id). The MC teardown record remains the review/authorization artifact;
wiring an operator-triggered teardown to `/v1/destroy` is a separate Mission Control change.

## Verification

- From MC with the client certificate, call `/health` and expect `{"status":"ok"}`.
- Without the client certificate, confirm Caddy rejects the TLS connection.
- With a valid certificate but invalid bearer credential, confirm
  `/v1/provision` returns `401`.
- From an address outside the MC firewall allowlist, confirm the broker is not
  reachable. Confirm the local broker port is not reachable from the network.
- Run one dummy-data dev-gate provisioning canary before allowing any customer
  creation, then exercise an explicit update, rollback, isolated backup/restore
  rehearsal, and tenant-isolation check.
- Confirm MC has no `ONEBRAIN_HETZNER_API_TOKEN` value and customer hosts have
  no broker URL, credential, certificate, or cloud token.

Record the certificate serial/expiry, firewall rule identifiers, broker image
digest, token-rotation owner, and canary evidence in the operational change.
See the
[production activation runbook](../../docs/production-activation-runbook.md)
for the complete MC and customer activation sequence.

# Dedicated Hetzner Broker Host

This bundle runs the only process that holds the Hetzner Cloud API token. It is
not Mission Control, a customer deployment, or an operator UI. It has no
database, customer data, OneBrain application secrets, or browser routes.

## Before activation

1. Create a dedicated Hetzner host outside every customer network.
2. Install Docker Compose and Caddy on that host.
3. Restrict the Hetzner Cloud Firewall to TCP 443 from Mission Control's fixed
   public IPv4 address. Do not open SSH except through a separate, reviewed
   break-glass path.
4. Copy `broker.env.example` to `broker.env`, make it root-owned `0600`, and
   set the Hetzner token there. The raw MC broker credential is not stored on
   the broker; store only its SHA-256 hash.
5. Install `Caddyfile` as the host Caddy configuration. Create
   `/etc/caddy/broker-tls/` with root-owned `server.crt`, `server.key`, and the
   MC client-certificate CA `mc-client-ca.crt`.
6. Set `HETZNER_BROKER_HOST` in Caddy's environment, validate the Caddyfile,
   then restart the Caddy service. The bundled configuration disables Caddy's
   admin API, so `caddy reload` is intentionally unavailable.
7. Set `ONEBRAIN_BROKER_IMAGE` to an immutable image digest that includes this
   broker code, then start the service:

   ```sh
   docker compose --env-file broker.env up -d
   ```

The container publishes only `127.0.0.1:8181`; Caddy is the sole network
ingress and requires a valid MC client certificate for every route.
The Caddyfile also enforces an SNI/Host match to prevent a protected host from
being reached through an unprotected TLS name.

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
its allowlists. It exposes only `/health` and `/v1/provision`; teardown is not
implemented.

## Verification

- From MC with the client certificate, call `/health` and expect `{"status":"ok"}`.
- Without the client certificate, confirm Caddy rejects the TLS connection.
- With a valid certificate but invalid bearer credential, confirm
  `/v1/provision` returns `401`.
- Run one dummy-data dev-gate provisioning canary before allowing any customer
  creation.
- Confirm MC has no `ONEBRAIN_HETZNER_API_TOKEN` value and customer hosts have
  no broker URL, credential, certificate, or cloud token.

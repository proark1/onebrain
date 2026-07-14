# Hetzner Broker Implementation Plan

## Goal

Replace the deliberately blocked remote-broker placeholder with a real,
out-of-process provisioning path. Mission Control will use a narrowly scoped
broker credential and optional client TLS material; only the broker host will
read the Hetzner API token.

## 1. Typed remote protocol and MC client

- Add JSON codecs for the existing frozen server, volume, DNS, and firewall
  request types and for `BrokerProvisionResult`.
- Add `RemoteHetznerBroker`, implementing the existing `HetznerBroker`
  protocol through one `POST /v1/provision` request.
- Use short, configurable timeouts; reject non-JSON, malformed, oversized, or
  unexpected responses as provisioning failures.
- Configure an HTTPS URL, broker credential, CA bundle, and MC client
  certificate/key on MC. The client never accepts or reads a Hetzner token.
- Update the broker factory and provisioner enablement so a remote URL selects
  the remote client without requiring `ONEBRAIN_HETZNER_API_TOKEN` on MC.

## 2. Broker process

- Add a separate FastAPI broker application with only `/health` and
  authenticated `/v1/provision`; it mounts no OneBrain customer, operator,
  fleet, or browser routes and uses no database.
- Read the Hetzner token and a hash-only MC broker credential from a dedicated
  broker settings model.
- Reuse the existing `InProcessHetznerBroker` only inside the broker process,
  with its existing idempotency and fleet-size cost cap.
- Return only the sanitized `BrokerProvisionResult`; never echo headers,
  tokens, cloud errors beyond a safe summary, cloud-init, or request secrets.

## 3. Broker-side validation

- Validate deployment identifiers, names, labels, locations, server types,
  image, data-volume size, DNS zone/label, pre-created firewall IDs, and SSH
  key IDs against broker-owned allowlists.
- Enforce the OneBrain fleet label and deployment-label consistency.
- Accept only default-deny firewall rules for TCP 80/443, with TCP 22 permitted
  only when broker configuration explicitly allows it.
- Enforce the Hetzner cloud-init size limit before any provider call.
- Reject all non-provision operations; destructive actions remain unimplemented.

## 4. Deployment assets

- Add a minimal broker container entrypoint, compose file, Caddy configuration,
  systemd/bootstrap guidance, and an environment example under `deploy/broker/`.
- Bind the broker app to loopback only. Caddy terminates TLS, requires the MC
  client certificate, and proxies only the two broker routes.
- Document a host firewall that allows TCP 443 only from MC's fixed public IP
  and denies public SSH by default.

## 5. Tests and verification

- Unit-test codec round trips, request shape, client TLS context selection,
  timeout/error handling, and factory selection.
- Test broker credential rejection and each allowlist boundary before asserting
  that the underlying fake Hetzner client receives no mutation.
- Test valid authenticated idempotent provisioning and cost-cap behavior.
- Test that logs and responses do not include the Hetzner token or MC broker
  credential.
- Run the focused broker/provisioner tests and full test suite. Do not deploy
  or configure production secrets until code review and tests pass.

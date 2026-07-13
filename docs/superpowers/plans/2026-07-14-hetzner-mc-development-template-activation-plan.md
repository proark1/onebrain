# Hetzner Mission Control and development-template activation plan

**Design:** `docs/superpowers/specs/2026-07-14-hetzner-mc-development-template-activation-design.md`

## 1. Correct the shared communication-image contract

- Update the Hetzner renderer so each rendered communication service receives a
  fixed `SERVICE` value: `api`, `widget`, `voice`, or `workers`.
- Preserve the existing module IDs, module health probes, service-key scope, and
  digest-pinned per-module release map.
- Add regression coverage that renders a full-stack compose file using one
  shared communication image digest and verifies all four service selectors.

## 2. Validate the full-stack release inputs

- Resolve immutable current image digests for OneBrain, assistant, and shared
  communication images.
- Construct an eight-module baseline image map that repeats the verified shared
  communication image digest for the four communication modules.
- Keep the release signature requirement unchanged: the manifest must be signed
  offline with the production release key before it can be active or provision a
  development gate.

## 3. Verify the existing Mission Control host

- Read only non-secret state from `onebrain-mc`: compose layout, running
  containers, deployed image references, database migration status, and the
  presence (not values) of required MC configuration.
- Create a rollback-safe deployment procedure that saves the previous
  digest-pinned compose image references before the MC upgrade.
- Never print or transfer the Hetzner API token, release private key, service
  keys, user passwords, or MC session secret.

## 4. Upgrade and configure Mission Control

- Publish the current OneBrain API, worker, and admin UI images from `main`.
- Upgrade the existing Hetzner MC host to the resulting pinned release and run
  its Alembic migration before starting its public API.
- Confirm `mc.onlyonebrain.com` returns healthy, operator-authenticated routes
  are available, and no unauthenticated control-plane data is exposed.
- Configure the MC-only Hetzner provider and trust settings through its
  protected environment. If the required Hetzner token or production
  verification public key is absent, stop before provisioning any server and
  report the missing configuration by name only.

## 5. Register the initial full-stack baseline

- Prepare the exact canonical release manifest from the verified image map and
  current migration revision.
- Obtain the required offline signature without placing the private key on MC,
  CI, the development host, or this repository.
- Register and activate the signed release through Mission Control, then verify
  its stored signature and full module coverage.

## 6. Provision the development customer host

- Invoke the fixed development-gate endpoint with `dry_run=true` and inspect its
  generated, fixed customer identity and full-stack inputs.
- Repeat with `dry_run=false` only after all preflight conditions are satisfied.
- Record only deployment metadata and the returned hostname. Do not expose the
  bootstrap password beyond its one-time secret mechanism.

## 7. Verify isolation and designate the gate

- Check public TLS and the OneBrain, assistant, and communication health paths.
- Confirm all four control-plane route groups return `404` from the customer
  host and customer container environments contain no fleet credentials.
- Confirm the root-owned agent emits a fresh, healthy, exact-version heartbeat.
- Designate the new host only when Mission Control returns no gate blockers.

## 8. Verification and shipment

- Run focused rendering tests, the full Python test suite, compile checks,
  rendered-compose validation, and Caddy adaptation validation.
- Commit and ship source changes using the repository shipping workflow.
- Leave customer rollout enforcement in report-only mode until a later explicit
  decision; successful gate designation never automatically deploys to a real
  customer.

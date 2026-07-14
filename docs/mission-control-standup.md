# Mission Control Standup Runbook

Mission Control (MC) is the private global super-admin control plane at
`mc.onlyonebrain.com`. It is not a customer workspace and is not a
customer-facing product surface.

## Preconditions

- A dedicated Hetzner host for MC, with separate Postgres/pgvector and backup
  policy.
- A public HTTPS name for authenticated machine-to-machine fleet traffic.
- An administrative access restriction: VPN or explicit source-IP allowlist for
  the MC UI and privileged operator routes.
- Offline production release-signing private key and a public verification key
  installed on the appropriate verifier hosts.
- A distinct development release verification key for the dummy-data dev gate.
- A separate private broker host before enabling MC-managed provisioning.

## Configure MC

Set production secrets in a root-owned environment file or equivalent secret
store. Do not commit them.

Required values include:

```text
ONEBRAIN_ENVIRONMENT=production
ONEBRAIN_OPERATOR_MODE=true
ONEBRAIN_OPERATOR_CONSOLE=true
ONEBRAIN_FLEET_PUBLIC_URL=https://mc.onlyonebrain.com
ONEBRAIN_FLEET_BASE_DOMAIN=<approved customer domain>
ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY=<offline-production-public-key>
ONEBRAIN_DEV_RELEASE_VERIFY_PUBLIC_KEY=<development-public-key>
ONEBRAIN_RELEASE_REGISTRY_ALLOWLIST=ghcr.io/proark1
ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true
ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=<MC-only-key>
```

Use strong session/authentication secrets, database credentials, and per-host
encrypted backups. The MC desired-state key is distinct from the offline
release-signing key.

Do **not** set a Hetzner API token on MC. Broker authentication and the broker
endpoint are configured only when the dedicated remote broker implementation is
deployed and validated.

## Validate before enabling rollouts

1. Run database migrations and confirm the app reports the expected migration
   head.
2. Confirm local and public `/health` checks return success over HTTPS.
3. Confirm the admin UI is reachable only through the intended restricted path.
4. Confirm MC accepts sanitized heartbeats but does not receive customer
   content, logs, prompts, documents, or credentials.
5. Confirm release verification rejects an unsigned, untrusted, or mutable
   image reference.
6. Confirm the development gate can report its full-stack health and immutable
   image digests.

## Operating rules

- MC may list deployment IDs, versions, release dates, health states, and
  rollout history.
- MC must not browse customer databases, documents, conversations, or support
  logs.
- Customer applications must not receive MC UI credentials or fleet routes.
- A customer rollout is always an explicit super-admin action after dev-gate
  verification.
- A broker failure blocks provisioning safely; it never grants MC direct cloud
  credentials as a fallback.

See [Mission Control architecture](mission-control-architecture.md) and the
[deployment guide](deployment.md) for the full boundaries.

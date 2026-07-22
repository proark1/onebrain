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
- A PostgreSQL owner/app-role setup that can migrate first and then serve all
  API and worker replicas with RLS enabled.
- Offline production release-signing private key and a public verification key
  installed on the appropriate verifier hosts.
- A distinct development release verification key for the dummy-data dev gate.
- A separate private broker host, MC egress address allowlist, and mTLS trust
  material before enabling MC-managed provisioning.

## Configure MC

Set production secrets in a root-owned environment file or equivalent secret
store. Do not commit them.

Required values include:

```text
ONEBRAIN_ENVIRONMENT=production
ONEBRAIN_OPERATOR_MODE=true
ONEBRAIN_OPERATOR_CONSOLE=true
ONEBRAIN_PROVISIONER_BACKEND=hetzner
ONEBRAIN_HETZNER_ALLOW_INPROCESS_BROKER=false
ONEBRAIN_HETZNER_BROKER_URL=https://<private-broker-host>
ONEBRAIN_HETZNER_BROKER_CREDENTIAL=<mc-only-broker-credential>
ONEBRAIN_HETZNER_BROKER_CLIENT_CERTIFICATE_FILE=/root/broker-tls/mc-client.crt
ONEBRAIN_HETZNER_BROKER_CLIENT_KEY_FILE=/root/broker-tls/mc-client.key
ONEBRAIN_HETZNER_API_TOKEN=
ONEBRAIN_VECTOR_STORE=pgvector
ONEBRAIN_RLS_ENFORCED=true
ONEBRAIN_DATABASE_URL=<shared-postgres-app-dsn>
ONEBRAIN_MIGRATION_DATABASE_URL=<postgres-owner-dsn>
ONEBRAIN_OPERATOR_DATABASE_URL=<postgres-owner-or-dedicated-operator-dsn>
ONEBRAIN_AUTH_SECRET=<unique-32-plus-character-session-secret>
ONEBRAIN_COOKIE_SECURE=true
ONEBRAIN_LOGIN_RATE_LIMIT_SECRET=<unique-32-plus-character-secret>
ONEBRAIN_DEPLOYMENT_ID=<mc-deployment-id>
ONEBRAIN_FLEET_URL=https://mc.onlyonebrain.com
ONEBRAIN_FLEET_KEY=<mc-fleet-heartbeat-key>
ONEBRAIN_FLEET_PUBLIC_URL=https://mc.onlyonebrain.com
ONEBRAIN_FLEET_BASE_DOMAIN=<approved customer domain>
ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY=<offline-production-public-key>
ONEBRAIN_DEV_RELEASE_VERIFY_PUBLIC_KEY=<development-public-key>
ONEBRAIN_RELEASE_REGISTRY_ALLOWLIST=ghcr.io/proark1
ONEBRAIN_RELEASE_REQUIRE_SIGNATURE=true
ONEBRAIN_RELEASE_REQUIRE_SIGNED_IMAGES=true
ONEBRAIN_RELEASE_REQUIRE_ROLLBACK_KIND=true
ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true
ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=<MC-only-key>
ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS=<served-public-key-set>
ONEBRAIN_FLEET_DESIRED_STATE_TTL_SECONDS=900
ONEBRAIN_FLEET_RECONCILE_SECONDS=60
ONEBRAIN_FLEET_WATCHDOG_SECONDS=60
ONEBRAIN_FLEET_LOW_ROOT_DISK_PERCENT=15
ONEBRAIN_FLEET_LOW_DATA_DISK_PERCENT=15
ONEBRAIN_FLEET_DATA_VOLUME_PATH=/mnt/onebrain-data
```

Use strong session/authentication secrets, database credentials, and per-host
encrypted backups. The MC desired-state key is distinct from the offline
release-signing key.

Do **not** set a Hetzner API token on MC. Configure the broker endpoint,
broker-scoped credential, and MC mTLS files only after the dedicated broker
host is deployed and validated. Configure trusted proxy CIDRs and hop count
only when a controlled proxy is the direct MC peer; otherwise leave forwarded
header trust disabled.

If MC uses LiteLLM embeddings, set `ONEBRAIN_EMBEDDING_DIM` deliberately with
the matching model configuration. The provider and the migrated pgvector schema
must return that exact dimension. A dimension change is a re-embedding
migration, never a rolling-startup adjustment.

## Validate before enabling rollouts

1. Run database migrations with the owner role before starting or scaling any
   API/worker replica, and confirm the app reports the expected migration head.
2. Start at least two API replicas. Confirm failed login attempts are limited
   across both replicas and that an untrusted forwarding header cannot choose a
   different client-address bucket.
3. Confirm local and public `/health` checks return success over HTTPS.
4. Confirm the admin UI is reachable only through the intended restricted path.
5. Confirm MC accepts sanitized heartbeats but does not receive customer
   content, logs, prompts, documents, or credentials.
6. Confirm release verification rejects an unsigned, untrusted, or mutable
   image reference.
7. Confirm the development gate can report its full-stack health and immutable
   image digests.
8. Verify the broker rejects a missing/invalid client certificate and an
   invalid broker credential, and accepts a valid MC request only from the
   approved source address.
9. If LiteLLM embeddings are enabled, confirm provider/schema-dimension
   preflight succeeds before traffic is accepted.

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
- A teardown approval is evidence collection only. Two independent approvals
  finish in `execution_disabled`; no MC route or broker action deletes a
  customer deployment.

## Host image reclaim

MC is deployed by hand — `docker compose … --force-recreate` against an edited
`/opt/onebrain/images.override.yml` — because it only accepts production-signed
releases on the auto path. That manual path never runs `update.sh`'s immediate
post-update prune, so every deploy leaves a superseded api+admin-ui+workers image
trio (~3.6 GB) behind, and the daily `onebrain-host-maintenance` sweep only reclaims
images older than 168 h — too lax for MC's cadence. Left unattended the root disk
fills and the next `--force-recreate` wedges on "no space left on device".

A dedicated MC-only unit, `onebrain-mc-image-prune.timer`, reruns the **same**
vetted, rollback-safe `onebrain-host-maintenance.sh` prune every 6 h with a short
48 h retention. It protects every image named by the compose file, both image
overrides, the last applied release, and every running/stopped container, and never
force-removes — so it can only reclaim genuinely superseded images. Newly rendered
MC boxes enable it at first boot. Tune the retention/cadence in the unit files under
`deploy/box/` if MC's deploy rate changes.

**Install on an already-running MC** (the units bake into cloud-init, so an existing
box needs them written once over its SSH/root access; the prune script itself is
pulled from the running API image, so only the two unit files are needed). Substitute
MC's real compose project (e.g. `onebrain-mc`) for `{{COMPOSE_PROJECT}}` in the
`.service`:

```bash
# write /etc/systemd/system/onebrain-mc-image-prune.{service,timer} from deploy/box/
systemctl daemon-reload
systemctl enable --now onebrain-mc-image-prune.timer
# verify it actually reclaims (the log must end in "complete", never "holding"):
systemctl start onebrain-mc-image-prune.service
journalctl -u onebrain-mc-image-prune.service -n 20 --no-pager
df -h /
```

A `holding` line means the script's data-volume gate did not pass; investigate the
`/mnt/onebrain-data` mount before relying on the timer. Always `df -h /` before a
manual MC deploy regardless.

See [Mission Control architecture](mission-control-architecture.md) and the
[deployment guide](deployment.md) for the full boundaries, then complete the
[production activation runbook](production-activation-runbook.md) before
enabling customer creation.

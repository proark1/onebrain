# Mission Control architecture

```
status:      draft v1
date:        2026-07-11
companion:   target-architecture.md (approved v2) — this document implements
             its control-plane, operator-root-of-trust, release-bundle and
             conformance sections for the fleet; section references (§N)
             point into that document.
scope:       the operator control plane: fleet registry, telemetry,
             config distribution, release bundles, ring rollouts,
             provisioning unification, operator identity, super-admin UI,
             analytics.
```

## 1. What Mission Control is, and is not

Mission Control (MC) is a separate Railway project with its own Postgres that
knows, for every customer deployment: that it exists, what versions it runs,
whether it is healthy, when it was last backed up, and aggregate usage counts.
It can provision new customer stacks, distribute operational config, and roll
pinned release bundles across the fleet in rings.

**The metadata-only boundary, stated up front (§1):** MC holds no customer
content. No message or document text, no titles, no prompts or completions,
no end-user names/emails/phone numbers, no per-record IDs, no stack traces or
free-text error strings, no secrets, tokens or DSNs from customer stacks.
Operator access to customer content stays where §10/§16 put it: break-glass,
executed inside the customer deployment, audited on both sides. MC hosts at
most the break-glass *case shell* (reason, target, timestamps), never proxied
content.

MC is off the serving path. If MC is down, every customer stack keeps
running; the cost is blind alerting, covered by one external dead-man ping.

**Pooled vs dedicated, noted without relitigating.** The user's decision is
dedicated-per-customer ("same setup for different customers, all their own
deployment"). Architecture v2 recommends pooled-default with dedicated as the
enterprise tier. MC takes no side: `control_deployments.deployment_type`
already encodes both (`shared_railway`, `dedicated_railway`, ...), and the
registry models deployment(1)→tenants(N) — a pooled deployment is one row
with N tenant rows, a dedicated one is the same shape with one tenant.
Today's shared OneBrain is literally a pooled deployment with two tenants
(both Assad's accounts). The fleet automation in sections 7–8 is what makes
dedicated-per-customer *operable* by one person: without pinned bundles and
ring rollouts, N dedicated stacks means N manual upgrades. Whichever tier
wins commercially, MC does not change.

## 2. Topology decision

**Chosen: a separate Railway project (`mission-control`) with its own
Postgres, running the existing OneBrain image in a new operator mode** —
not a new repo/service, and not the status quo (control-plane tables inside
the shared customer-serving DB).

* `ONEBRAIN_OPERATOR_MODE=true` registers only: the control-plane store,
  `/api/operator/*`, `/api/provisioning/*`, and a new `/api/fleet/*` router.
  Customer-serving routers (intake, retrieval, assistant, chunks) are never
  registered, so the content tables alembic creates sit provably empty.
  The metadata-only rule is enforced by absent code paths, not policy.
* Everything ports verbatim: the 0005 `control_*` schema, the
  `ControlPlaneStore` protocol with its tested `plan_update` /
  `start_rollout` / terminal-immutability state machine
  (`app/controlplane/postgres.py`), the 0006 `provisioning_runs` +
  `one_time_secret_envelopes` machinery, and the `/api/operator/*` router.
  The store is DSN-swappable via `app/controlplane/factory.py` — this is a
  DSN change plus a mode flag, not a rewrite.
* `onebrain-web` deploys beside it as the MC UI: `operator-panel.tsx` (5
  tabs, full lifecycle) and `cockpit-panel.tsx` already exist and are typed
  against exactly these endpoints (`src/lib/onebrain-client.ts`).
* **Customer stacks are demoted, not deleted:** `ONEBRAIN_OPERATOR_CONSOLE=false`
  (default in customer builds) stops shipping fleet endpoints and operator UI
  pages to customers. Each deployment keeps its local
  `GET /api/operator/observability` (its own stats only). This closes today's
  real hole: `_require_admin` (operator.py:297) lets any customer admin of
  the shared deployment read the whole fleet, and the operator console ships
  into every customer stack.
* Control-plane rows migrate out of the shared OneBrain DB into MC's DB. At
  N=2 deployments, re-keying by hand in the MC UI beats a migration script.

Honest cost: the MC DB carries empty content tables from shared migrations
(mitigated: nothing can write them), and MC is one more deployment to run.
Named loser: a purpose-built `mc-api` service — cleaner surface, but weeks of
re-implementing a tested state machine. Exit seam: the store protocol + DSN
factory make later extraction to a dedicated service mechanical; take it only
when operator mode measurably hurts.

## 3. Registry and data model

Ported unchanged from 0005/0006: `control_deployments`,
`control_deployment_modules`, `control_release_manifests`, `control_backups`,
`control_health_checks`, `control_rollouts`, `provisioning_runs`,
`one_time_secret_envelopes`.

New tables (one MC migration):

```sql
customers (
  id TEXT PK, name TEXT, slug TEXT UNIQUE,
  tier TEXT CHECK (tier IN ('pooled','dedicated')),
  status TEXT DEFAULT 'active', contact_email TEXT, notes TEXT,
  created_at TIMESTAMPTZ
)

-- pooled: one deployment row, N tenant rows; dedicated: exactly one tenant.
deployment_tenants (
  deployment_id TEXT REFERENCES control_deployments(id),
  tenant_ref TEXT,            -- opaque: OneBrain account_id / Comm tenant_id
  customer_id TEXT REFERENCES customers(id),
  status TEXT DEFAULT 'active',
  PRIMARY KEY (deployment_id, tenant_ref)
)

fleet_keys (                  -- heartbeat credentials, hashed like service keys
  id TEXT PK, deployment_id TEXT REFERENCES control_deployments(id),
  key_hash TEXT, created_at TIMESTAMPTZ,
  rotated_from TEXT NULL, revoked_at TIMESTAMPTZ NULL
)

heartbeats (                  -- append-only telemetry log, 90-day retention
  id BIGSERIAL PK, deployment_id TEXT, received_at TIMESTAMPTZ,
  contract_version TEXT, payload JSONB   -- schema-validated before insert
)
heartbeat_rollups (deployment_id TEXT, day DATE, aggregates JSONB,
                   PRIMARY KEY (deployment_id, day))

desired_configs (             -- versioned desired-state config per deployment
  deployment_id TEXT, version INT, doc JSONB,
  updated_at TIMESTAMPTZ, updated_by TEXT,
  PRIMARY KEY (deployment_id, version)
)

alerts (
  id TEXT PK, dedup_key TEXT, type TEXT, severity TEXT,
  deployment_id TEXT NULL, tenant_ref TEXT NULL, detail JSONB,
  opened_at TIMESTAMPTZ, acked_at TIMESTAMPTZ NULL,
  resolved_at TIMESTAMPTZ NULL, last_notified_at TIMESTAMPTZ NULL
)
-- UNIQUE (dedup_key) WHERE resolved_at IS NULL → one open alert per condition

control_fleet_rollouts (      -- fleet-level ring orchestration (section 7)
  id TEXT PK, bundle_version TEXT REFERENCES control_release_manifests(version),
  current_ring TEXT,
  status TEXT CHECK (status IN ('queued','rolling','soaking','halted','complete')),
  soak_until TIMESTAMPTZ NULL, halted_reason TEXT NULL, created_by TEXT
)

operator_audit (              -- append-only trigger (0010 pattern) + hash chain
  seq BIGSERIAL PK, at TIMESTAMPTZ, actor TEXT, action TEXT,
  target_type TEXT, target_id TEXT, meta JSONB,
  prev_hash BYTEA, hash BYTEA   -- SHA256(prev_hash || canonical_json(row))
)

recovery_cases (              -- sole-Owner lockout / break-glass case shells
  id TEXT PK, deployment_id TEXT, account_ref TEXT,
  kind TEXT CHECK (kind IN ('sole_owner_lockout','break_glass_shell')),
  reason TEXT, verification_method TEXT, opened_at TIMESTAMPTZ,
  dispatched_run_id TEXT NULL, notified_at TIMESTAMPTZ NULL,
  closed_at TIMESTAMPTZ NULL, outcome TEXT NULL
)
```

`control_deployments` gains latest-state columns for cheap fleet overview:
`customer_id FK`, `base_urls JSONB` (public health URLs per service),
`railway_project_id`, `heartbeat_interval_s INT DEFAULT 300`,
`last_heartbeat_at`, `last_reported_version`, `last_reported_migration`,
`heartbeat_status` ('ok'|'stale'|'missing'|'never'),
`desired_config_version INT`, `acked_config_version INT`.

`provisioning_runs` generalizes with a `kind` column
(`provision` | `upgrade` | `rollback` | `recover` | `suspend` | `offboard`);
monotonic status ranks, terminal immutability, retry, and one-time-secret
envelopes carry over unchanged (`app/provisioning/runs.py`).

The `_readiness` derivation (operator.py:448-461) extends with heartbeat
state: `never_reported / heartbeat_stale / version_drift` on top of
`healthy / rollout_failed / backup_failed / ...`.

Every mutating MC endpoint writes `operator_audit` in the same transaction as
its mutation; audit-insert failure rolls back the action. A nightly job
re-verifies the hash chain; a gap raises a P1 alert. External write-once
sink (S3 Object Lock): deferred, named trigger = first external customer
signs — until then the only party the log protects the operator from is
himself.

## 4. Telemetry: push heartbeats + credential-free pull fallback

**Direction decided by blast radius: PUSH.** A credentialed puller would make
MC a vault of N credentials into customer data planes — a compromised MC
reads the fleet. A pusher gives each stack one key valid for exactly one
write-only endpoint pinned to its own `deployment_id`; a compromised stack
can forge only its own telemetry. MC holds **zero** credentials into customer
stacks for telemetry.

**Sender:** one combined heartbeat per deployment set. onebrain-api runs a
background asyncio task (gated by `ONEBRAIN_FLEET_URL` + `ONEBRAIN_FLEET_KEY`)
every `heartbeat_interval_s` (default 300s):

1. Gathers local aggregates with the same COUNT queries
   `/api/operator/observability` already runs.
2. Polls sibling services over Railway project-private networking
   (Communication `/health` + `/ready`, workers `/health`, assistant
   `/health/ready`) — one credential, one payload per stack, not one per
   service.
3. `POST /api/fleet/heartbeat` with `Authorization: Bearer <fleet key>`
   (hashed at rest in `fleet_keys`; the same proven pattern as the
   provisioning callback key — bearer over HMAC/mTLS because TLS already
   covers the channel and mTLS is not practical on the Railway edge; the
   security lens's HMAC proposal is resolved in favor of bearer for symmetry
   with existing key handling).
4. Applies the desired-config version returned in the response (section 6).

The reporter never crashes the app: log, retry next tick. If onebrain-api is
down, no heartbeat is sent — exactly the watchdog's signal.

**Fallback: credential-free pull.** On a missed heartbeat, MC's watchdog
polls the deployment's public unauthenticated `/health` endpoints from
`base_urls` (all already exist fleet-wide). This distinguishes "stack down"
from "heartbeat path broken" at zero credential cost — `down` vs
`heartbeat_only` alerts. MC never does credentialed polling of
`/api/operator/observability`.

**Payload — contract `fleet.v1`,** carried contract-version rejected on
mismatch (§20). Every field is a version string, enum, boolean, timestamp,
or integer count. Pydantic `extra="forbid"`; failures are 422 + logged;
repeated violations open a `schema_violation` alert.

```json
{
  "contract_version": "fleet.v1",
  "deployment_id": "dep_ab12",
  "sent_at": "2026-07-11T10:05:00Z",
  "stack": {"release_version": "2026.07.1", "git_sha": "89a6c6d",
            "migration_revision": "0010", "uptime_seconds": 86400},
  "modules": [
    {"module_id": "onebrain-api", "version": "2026.07.1", "health": "ok",
     "detail": {"db_ok": true, "queue_pending": 3, "queue_failed_24h": 0}},
    {"module_id": "communication-api", "version": "1.4.2", "health": "degraded",
     "detail": {"db_ok": true, "redis_ok": false}},
    {"module_id": "assistant-service", "version": "0.9.0", "health": "unreachable"}
  ],
  "aggregates": {"accounts": 1, "users": 12, "spaces": 9,
    "app_installations": 4, "documents": 120, "chunks": 15230,
    "conversations": 480, "messages_24h": 210,
    "tokens_in_24h": 1200000, "tokens_out_24h": 410000,
    "jobs_pending": 3, "jobs_failed_24h": 0, "auth_failures_24h": 2,
    "http_5xx_24h": 0, "storage_bytes": 1073741824},
  "tenants": [
    {"tenant_ref": "acc_7f3a", "status": "active", "users": 12,
     "messages_24h": 210}
  ],
  "backup": {"last_success_at": "2026-07-11T02:00:00Z", "kind": "pg_dump",
             "size_bytes": 52428800},
  "config": {"applied_version": 14},
  "security": {"rls_enforced": true, "cookie_secure": true, "pii_phase": "phase2"},
  "errors": [{"class": "IntegrityError", "count_24h": 2}]
}
```

**Forbidden across the boundary, enumerated:** content or titles of any kind;
end-user PII (names, emails, phone numbers); space names; per-record IDs or
ID lists; prompts/completions; stack traces or error *messages* (error class
+ count only — messages embed payloads); end-user IPs; secrets, tokens,
DSNs; any free-text field not on the enum list. Customer company names live
only in MC's own `customers` registry; heartbeats carry opaque refs. There
is deliberately **no per-user field** in the contract — per-employee
analytics is structurally impossible, not policy-excluded (section 11).

**Ingest processing:** insert `heartbeats` row → update latest-state columns
on `control_deployments` / `control_deployment_modules` → auto-resolve open
`heartbeat_missed` alert → run drift checks. Nightly job folds rows older
than 90 days into `heartbeat_rollups` and prunes. At 10 customers × 288
heartbeats/day ≈ 2.9k rows/day: plain Postgres, no TSDB.

**Registration handshake (closes survey gap #9):** provisioning mints a
`fleet_keys` row before dispatch and passes the plaintext key as a workflow
input; the workflow sets `ONEBRAIN_FLEET_URL`/`ONEBRAIN_FLEET_KEY` in
`common_vars`. First heartbeat flips `heartbeat_status` from 'never' to
'ok'. The two existing deployments are backfilled by hand (mint key in UI,
set two env vars). Rotation: mint new key (`rotated_from` set), set var,
revoke old on first success with the new key. Auth failure on a revoked/
unknown key opens `fleet_key_auth_failure` — the compromise detector.

**Alert catalog** (all checkers run in MC's worker; state = `alerts` rows
with the open-alert dedup index; delivery = email via SMTP/Resend + one push
channel (ntfy/Telegram) + cockpit badge; notifier loop resends with backoff —
delivery failure degrades to "visible on next login", never silence):

| Alert | Trigger |
|---|---|
| `heartbeat_missed` | watchdog (1 min): `now - last_heartbeat_at > 3 × interval` |
| `service_unhealthy` | module health != ok for 2 consecutive; or public /health poll fails |
| `version_drift` | reported version != what MC believes it deployed |
| `migration_mismatch` | post-rollout: `migration_revision != manifest.migration_to`; blocks rollout success |
| `backup_stale` | hourly: `backup.last_success_at` older than 26h |
| `config_drift` | hourly: desired != acked config version for > 2h |
| `rollout_stuck` | hourly: rollout non-terminal > 1h |
| `schema_violation` / `fleet_key_auth_failure` | on request |

**Who watches the watcher:** an external free uptime pinger (UptimeRobot /
healthchecks.io) on MC's `/health` that emails Assad. That is the entire HA
story, deliberately — MC down costs telemetry, not customers.

## 5. Config distribution ("updates into projects" that are config, not code)

Desired-state, pull-and-ack, riding the heartbeat channel. MC never opens a
connection into a customer stack.

* Operator edits config in the MC UI →
  `PUT /api/operator/deployments/{id}/config` writes a new `desired_configs`
  row (`version = max+1`), bumps `desired_config_version`.
* The next heartbeat *response* carries the doc + version:
  `{"received": true, "poll_interval_seconds": 300,
    "desired_config_version": 15, "desired_config": {...}}`
* The stack applies keys against a **registered allowlist** in its own
  `runtime_config` table: feature flags, log level, alert thresholds, module
  enablement, heartbeat interval — non-secret operational knobs only.
  Unknown keys are ignored and reported back as unapplied; MC cannot push
  arbitrary behavior into a stack that doesn't understand it.
* Ack via the next heartbeat's `config.applied_version`; MC records
  `acked_config_version`; drift > 2h opens `config_drift`. A stack that
  cannot parse the doc keeps last-known-good and reports the failure as an
  error class.
* **Never secrets through this path**, in either direction. Secret rotation
  (LLM keys, DSNs) goes through Railway variables set by the deploy
  pipeline's deploy-scoped identity (section 8).
* **Code deploys are not config.** Those are rollouts (section 7). Cost
  accepted: config lands within one heartbeat interval, not instantly —
  worth keeping MC's inbound-credential count at zero.

## 6. Release bundles

**A bundle is one `control_release_manifests` row, upgraded from bookkeeping
to the deployable unit.** `modules` JSONB gains a required per-module shape:

```json
{
  "onebrain-api":      {"version": "2026.07.1", "git_sha": "89a6c6d",
                        "image": "ghcr.io/assad/onebrain-api@sha256:..."},
  "onebrain-workers":  {"version": "2026.07.1", "git_sha": "89a6c6d",
                        "image": "ghcr.io/assad/onebrain-workers@sha256:..."},
  "onebrain-admin-ui": {"version": "2026.07.1", "git_sha": "89a6c6d",
                        "image": "ghcr.io/assad/onebrain-admin-ui@sha256:..."},
  "communication-api": {"version": "2026.07.1", "git_sha": "<comm sha>",
                        "image": "ghcr.io/assad/communication-api@sha256:...",
                        "migration": "<comm migration head>"},
  "assistant-service": {"version": "2026.07.1", "git_sha": "<pa sha>",
                        "image": "ghcr.io/assad/assistant-runtime@sha256:..."}
}
```

* **Image digests, not tags, are the pin.** What conformance tested is
  byte-identical to what every customer runs. The mutable `*_IMAGE` repo
  secrets and `railway up`-from-branch-HEAD are retired from the
  customer-facing path (audited gap #1 closed).
* `migration_from`/`migration_to` (already in 0005) hold the OneBrain
  alembic heads this bundle expects/produces.
* **Versioning: calendar bundles** — `fleet-v2026.07.1` (year.month.serial).
  A solo operator ships bundles, not semver promises; compatibility
  semantics live in the contract-version handshake, not the number. The same
  tag is pushed to all three repos (onebrain, assaddar-ai-communication,
  personalassistant), so every `git_sha` is resolvable forever.
* A hotfix is a full new bundle (`fleet-v2026.07.2`, per §20's no-partial-
  hotfix rule); unchanged components carry their digests forward, so a
  one-line fix costs one rebuild plus cache hits.

**Cutting a bundle — `cut-release.yml`** (onebrain repo, `workflow_dispatch`):

1. Inputs: `bundle_version`, per-repo refs (default main HEAD),
   `callback_url`, `callback_key_id`.
2. Resolve refs → SHAs; push `fleet-v...` tag to all three repos
   (cross-repo PAT scoped to contents:write on exactly those repos).
3. Build & push the images to GHCR (buildx, cache-from previous bundle);
   capture digests.
4. **Conformance gate (§20):** `docker compose` the pinned images with a real
   Postgres (RLS enabled); run the cross-repo checks — shared allow/deny
   fixture matrix; `assistant.v1` handshake test (mismatched contract-version
   must be rejected at connect); OneBrain boots-and-migrates from a
   `migration_from` snapshot DB; Communication `start-service.mjs` migrates
   under its advisory lock against the same stack; all `/health` + `/ready`
   green. Additionally: boot the *previous* bundle's OneBrain image against
   the *new* migrated DB — if it healthchecks, the manifest gets
   `rollback_plan: "redeploy-previous-safe"`, else `"forward-only"`.
5. Green → POST the manifest to `POST /api/operator/releases` (exists,
   operator.py:786) with `status: "active"`. Red → post as `draft` with the
   failure summary; nothing partial is deployable because deploys only read
   *active* manifests. Callback failure is harmless: images are keyed by
   digest in GHCR and re-POSTing is idempotent (version is PK).

## 7. Fleet upgrades: deploy workflow, rings, halt, rollback

**One deploy path — `deploy-bundle.yml`** (reusable `workflow_call`), the
single place that knows how to put a bundle onto a Railway project. Both
day-0 provisioning and day-2 upgrades call it; they cannot drift.

* Inputs: `deployment_id`, `railway_project_id`, `manifest_json`,
  `installed_modules` (from `control_deployment_modules`), `callback_url`,
  `callback_key_id`, `run_id`, `dry_run`.
* Credential: `environment: dep-{deployment_id}` — one GitHub Environment
  per deployment holding one secret, a **Railway project-scoped token** with
  deploy rights on that project only. The deploy-only machine identity of
  §16; the workspace token never appears here (HIGH finding closed).
* Per service (Railway GraphQL, project-token auth):
  `serviceInstanceUpdate(source.image = <digest>)` → deploy → health-gate
  loop (reuse provision-customer.yml's 300s smoke pattern).
* **Deploy order with gates, halt on first failure:**
  1. `onebrain-api` (alembic migrates on boot) → gate `GET /health`
  2. `onebrain-workers` + `onebrain-admin-ui` in parallel → gate their
     `/health`
  3. `communication-api` (sole migrator under advisory lock) → gate
     `GET /ready` (503s on DB down — a real gate; contract enforcement makes
     it double as the compatibility check: a rejected contract-version never
     goes ready)
  4. Communication workers, then `assistant-service` → gate `/health/ready`
  5. Final callback: per-service `{module_id, deployed_digest, healthy}`
     array + overall status.
* Mixed state is recorded, never hidden: `control_deployment_modules` rows
  update per module from the callback, so intra-deployment version
  divergence is visible in the fleet overview.
* Callbacks reuse the provisioning-run contract verbatim (Bearer key
  verified against hash, monotonic ranks, terminal immutability, retry).
* `upgrade-customer.yml` is a ~20-line wrapper: optional pre-upgrade backup
  (below) → call `deploy-bundle.yml`.

**Pre-upgrade backup gate.** `plan_update` already blocks a schema-changing
rollout unless the latest backup is `success`
(postgres.py:294-301, `backup_required_for_schema_update`) — keep it as the
authoritative gate, and feed it honestly: when
`migration_to != current_migration`, `upgrade-customer.yml` first triggers a
Railway Postgres backup via the project token, waits, and reports it in the
callback so MC writes a real `control_backups` row before deploying. No DSN
enters pipeline scope (§16).

**Ring rollout.** Rings already exist (`base.py:23`):
`internal` = Assad's own accounts/deployments — the §16 canary;
`pilot`/`early`/`stable` = customers in sequence; `manual` = excluded
(contractually pinned customers). Two levels of state:

* Per-deployment: existing `control_rollouts`; `update_rollout_status('success')`
  already bumps versions atomically — and the post-deploy heartbeat now
  *verifies* it (`reported_version == target`, `migration == migration_to`),
  closing the loop the state machine currently takes on faith.
* Fleet: `control_fleet_rollouts` (section 3), driven by an idempotent
  60-second tick inside MC (DB is source of truth; restarts safe; no
  scheduler service at this fleet size). Each tick, per non-terminal fleet
  rollout:
  1. Ring has un-upgraded deployments → take the **next one only**
     (sequential): `plan_update` (backup + module-coverage gates) →
     `start_rollout` → create `kind=upgrade` run → dispatch
     `upgrade-customer.yml` via the existing `GitHubWorkflowDispatcher`.
  2. Failure callback, or no callback within 45 min (watchdog) → rollout
     `failed`, fleet rollout `halted` with reason. **Nothing auto-proceeds
     past a halt**; resume is an explicit operator action.
  3. Ring complete → `soaking`; `soak_until = now + soak_hours` (24h after
     `internal` — §16's delayed fan-out window; 4h between customer rings).
     Any red health for a just-upgraded deployment during soak → halt.
  4. Soak passed, health green → advance ring.
* Trigger: "Roll out fleet-v2026.07.1" button →
  `POST /api/operator/fleet-rollouts {bundle_version}`. Manual
  per-deployment rollouts (existing UI flow) remain and bypass the fleet row.
* Sequential over parallel is deliberate: at 2–10 customers it costs minutes
  and makes halt-before-spread trivially correct. A future pooled deployment
  is one ring member whose upgrade touches N tenants — place it late in
  `stable`; zero new machinery.

**Rollback policy — default is roll forward.** Both migrators are
forward-only; pretending otherwise silently eats data.

* No schema change between bundles → rollback = redeploy previous bundle
  (`kind=rollback` run; every prior bundle's digests live forever in the
  manifest table). The fast path; covers most bad releases.
* Schema changed → redeploy-previous is allowed **only** on manifests the
  conformance gate marked `redeploy-previous-safe` (expand-contract proven).
  MC refuses rollback dispatch on `forward-only` manifests.
* `forward-only` + broken release → fix forward: cut `fleet-vN+1` (cheap).
  The halted ring caps exposure at one deployment.
* Data corruption → restore the pre-upgrade Railway backup + redeploy
  previous bundle. Loses writes since backup; manual, documented-reason,
  break-glass-adjacent, never automated.

## 8. Day-0 = day-2: provisioning unification

`provision-customer.yml` is refactored into two jobs; the audited gaps close
in one move:

* **Job 1 "create-infra"** — the only job allowed the workspace-capable
  Railway token, quarantined in its own GitHub Environment
  (`fleet-provisioner`), used for nothing else: `railway init`, add
  Postgres/Redis and module datastores, mint bootstrap secrets, set env
  vars. It also mints the Railway **project token** and writes it into the
  new `dep-{deployment_id}` GitHub Environment (GH API), and mints +
  injects the fleet heartbeat key (section 4).
* **Job 2** calls `deploy-bundle.yml` with the `initial_version` manifest.
  Day-0 deploys the same pinned bundle a day-2 upgrade would — "day-0 only"
  (gap #3) and "no pinning" (gap #1) close together.
* **Shared GEMINI_API_KEY (gap #2):** create-infra stops injecting the org
  secret. Per customer: a dedicated LLM key (one key per per-customer GCP
  project — manual 5-minute onboarding step at ≤10 customers, scriptable
  later), set once into that project's Railway variables via the project
  token. MC stores only a fingerprint (`llm_key_ref`: last-4 + created_at) —
  never the value. Rotation = a later `rotate-secret.yml` (project token,
  `variableUpsert` + redeploy).
* **Module runtime deps (gap #4):** create-infra provisions the module
  databases/Redis the bundle's modules declare, and the bundle deploys real
  pinned images — no more "pending code" placeholder services.
* IdP bootstrap and automated backup/alerting setup remain provisioning
  TODOs tracked against target-architecture §2/§18; they are workflow steps
  added to create-infra when those workstreams land, not MC features.

## 9. Security and identity

**Operator authentication (the super-admin).** MC is a OneBrain deployment
with exactly one account ("operator") and one admin user (Assad).
`_require_admin` now *means* operator because no customer principal exists
in this DB — the missing operator-vs-customer-admin distinction resolves
structurally, not with new RBAC. Layered on top:

* Phase 1: existing OneBrain admin auth + optional Cloudflare Access (free
  tier) or IP allowlist in front of MC.
* Phase 2 (before the rollout executor gives MC buttons deploy power):
  **WebAuthn passkey** primary factor (py_webauthn; YubiKey/Windows Hello),
  TOTP fallback (Fernet-encrypted secret, same cipher pattern as
  `OneTimeSecretCipher`); server-side revocable sessions (Postgres rows,
  12h absolute / 30min idle, `httpOnly; Secure; SameSite=Strict` — not the
  stateless-HMAC mistake §21 names); **step-up** — dangerous actions
  (rollout beyond `internal`, halt/rollback, provision/suspend/offboard,
  recovery case, credential rotation) require a fresh passkey assertion
  within 10 minutes.
* Registration is not self-service: one-time `MC_BOOTSTRAP_TOKEN` env var on
  first boot, then deleted. Lockout recovery chains to the actual root of
  trust — the Railway + GitHub accounts under hardware-key MFA (§16). MC
  never pretends to be the root of trust; it hangs off it.
* No IdP for one human; revisit at first hire (multi-operator RBAC and
  approval chains are theater for a solo operator — §16 verbatim).

**Credential inventory.** MC holds (all near-worthless if MC is fully
compromised):

1. One fine-grained GitHub PAT, `actions:write` on the onebrain repo only —
   to dispatch workflows. MC can *request* a deploy of an already-active
   manifest; it cannot deploy anything itself.
2. Hashed callback bearer keys (inbound, existing pattern).
3. Hashed fleet heartbeat keys (inbound verification only).
4. Its own SMTP/Resend key + one push-channel token.

MC never holds: Railway tokens of any scope (they live in per-deployment
GitHub Environments; workspace token only in `fleet-provisioner`); customer
DSNs; customer service keys; customer admin passwords or session secrets;
customer LLM/provider keys; IdP admin credentials. There is **no
MC→customer-data-plane credential at all.** The full inventory in one
sentence: each stack holds 1 write-only key pointing at MC; MC holds 0 keys
pointing at stacks; the pipeline holds deploy-only tokens — that is the
German-B2B privacy story.

Content-bearing surfaces MC explicitly does not touch: Railway logs
(inspected only in the Railway console under the hardware-MFA account, per
§16 consent rules) and customer databases (break-glass only, executed inside
the deployment, never proxied).

**Sole-Owner lockout recovery (§16 flow, concrete):**

1. Customer contacts operator out-of-band; identity verified against
   contract-time records (registered recovery contact).
2. Operator opens a `recovery_cases` row (step-up required, audited).
3. MC dispatches a `kind=recover` run — a job executed **inside the
   customer deployment's trust domain** that mints a one-time short-TTL
   owner-reset credential returned via the existing
   `one_time_secret_envelopes` Fernet path (single read, TTL-burned).
   Identity realm only; no content row touched; MC never sees the credential.
4. Mandatory notification, enforced: the job emails every account
   admin/Owner and writes a customer-visible audit banner; a case cannot
   close with `notified_at` null.
5. Both sides audit: MC `operator_audit` + the deployment's own stream.
   When the shared IdP lands, step 3 becomes an IdP org-recovery action;
   nothing else changes.

General break-glass: MC hosts the case shell (reason, narrow target, expiry,
§10 pre-access notification + activation-delay timer, suppressible only by
an audited legal-hold reason); the access itself and its per-record audit
(§13 category c) happen inside the customer deployment.

## 10. Super-admin UI

Lift `operator-panel.tsx`, `cockpit-panel.tsx`, `onebrain-client.ts` from
onebrain-web; deploy in the MC project; customer stacks get
`ONEBRAIN_OPERATOR_CONSOLE=false`. Pages:

1. **Fleet overview** (home): one row per deployment — customer, tier badge,
   ring, deployed vs reported version (drift highlighted), migration rev,
   last-heartbeat age, readiness chip (extended `_readiness`), open-alert
   count. Pooled rows expand to tenants. Data: latest-state columns ×
   `deployment_tenants` × `alerts`.
2. **Customer registry + lifecycle**: existing provision form (bundle,
   brand theme, tier, ring) driving `kind=provision` runs; one-time
   bootstrap-secret read; suspend = step-up → dispatched in-deployment job
   (never a DB write from MC); offboard = audited checklist (export bundle,
   teardown run, key-destruction attestation, §18 backup-expiry timer).
3. **Release manager**: cut/inspect manifests, promote
   internal → pilot → early → stable with soak timers and health gates,
   HALT button (step-up), rollback = new rollout targeting the previous
   active manifest (refused on `forward-only`).
4. **Analytics**: fleet totals + per-customer cards from `heartbeat_rollups`
   — 30/90-day sparklines (section 11).
5. **Alerts inbox**: open/acked/resolved, ack-with-note (audited). Alerts
   are rows first, notifications second.
6. **Audit log**: filterable `operator_audit` read + chain-verification
   status indicator.
7. **Recovery/break-glass cases**: open, track, close per section 9.

## 11. Analytics

**Allowed in MC (metadata — counts, sums, versions, outcomes only):**
active users (a count, not a list), conversations, messages per channel,
tokens in/out and derived estimated cost (price table in MC config),
documents/chunks/storage bytes, job queue depth and failure counts,
auth-failure and 5xx counts, module versions, backup/health/rollout
outcomes, uptime — per deployment and per tenant. Everything in
`OperatorObservabilityOut` qualifies.

**Never in MC:** message/conversation text, subjects, excerpts; document
titles/filenames (titles are content); prompts/completions; embeddings;
end-customer contact identifiers; free-form error strings; Railway log lines.

**Deliberately excluded though technically metadata: per-employee activity**
(per-user message counts, per-user token usage). That is personal data of
the customer's employees — §87(1)(6) BetrVG co-determination territory.
Granularity floor in MC = account/tenant. Per-employee views belong in the
customer's own admin UI under the customer's governance. Enforcement is
structural: `fleet.v1` has no per-user field; adding one is a contract-
version bump reviewed against this rule.

Retention: raw `heartbeats` 90 days; `heartbeat_rollups` indefinitely
(aggregate by construction). Plain Postgres; no TSDB.

## 12. Anti-scope: what we do not build at 2–10 customers

Each exclusion names its reversal seam; none blocks the §1 fleet
preconditions.

* **Prometheus/Grafana/OpenTelemetry/TSDB** — a monitoring platform is
  itself a service to operate; Postgres rows + cockpit carry this fleet
  with headroom. Seam: versioned `fleet.v1` + append-only heartbeats table.
* **Credentialed pull into customer operator endpoints** — recreates the
  N-credential vault the push decision exists to avoid; richer inspection
  goes through break-glass, not a standing poller.
* **A CD platform** (ArgoCD/Flux/Spinnaker) — GitHub Actions + Railway API +
  Postgres rows is the whole engine.
* **Blue-green / traffic-splitting within a deployment** — Railway
  redeploy-with-healthcheck is the cutover; the canary is a whole
  deployment (ring `internal`), not a traffic percentage.
* **Parallel rollout fan-out** — sequential costs minutes and makes
  halt-on-failure trivially correct.
* **Automated down-migrations** — both migrators are forward-only; a fake
  reverse path silently eats data. Roll forward + break-glass restore is
  the honest set.
* **Secret broker / Vault** — Railway envs + GitHub Environment secrets are
  the vault at this scale; a broker is another root of trust to run solo.
* **Auto-remediation** (auto-restart, auto-rollback from telemetry) — every
  automated recovery path is an automated way to make things worse
  unobserved; halt-and-page the one human.
* **Multi-operator RBAC / SSO / approval workflows in MC** — self-approval
  gates are theater (§16); revisit at first hire.
* **MC high availability / multi-region** — MC down costs alerting, not
  customers; the external dead-man ping is proportionate.
* **Log aggregation into MC** — Railway logs can contain customer content;
  ingesting them breaches the metadata-only rule for a convenience.
* **Billing/metering engine** — token counters feed a manually issued
  invoice; billing v1 is a spreadsheet fed by the analytics page.
* **Paging platform** (PagerDuty) — email + one push channel + dead-man.
* **Per-employee analytics or any cross-fleet content search** —
  structurally excluded from the contract.
* **Artifact signing/SLSA/SBOM pipelines** — digest pinning + pinned
  lockfiles with dependency-diff review (§16) is proportionate; revisit on
  a customer security questionnaire.
* **A separate MC repo/microservice; a fleet-releases repo or monorepo** —
  operator mode + three repos with one synchronized tag is enough; extract
  only when it measurably hurts.
* **`dedicated_server` / `customer_owned` executors** — enum values are
  reserved (base.py:24); implement when someone buys them.
* **Streaming/WebSocket dashboards** — 5-minute heartbeats + the existing
  auto-refresh suffice.
* **External write-once audit sink** — deferred; named trigger: first
  external customer signs.

## 13. Build plan

### Phase 1 — registry + heartbeat + read-only dashboard (~4–5 days)

Everything lands in the **onebrain repo** unless noted.

* **Day 1 — stand up MC.** New Railway project `mission-control`: Postgres +
  onebrain-api with `ONEBRAIN_OPERATOR_MODE=true` in `app/main.py`
  (registers only operator/provisioning/controlplane/fleet routers); deploy
  onebrain-web beside it pointed at MC. Create the single operator
  account/admin. Hand-enter the 2 existing deployments as `customers` +
  `control_deployments` + `deployment_tenants` rows. Flip
  `ONEBRAIN_OPERATOR_CONSOLE=false` on the customer-serving deployment.
* **Day 2 — heartbeat ingest.** One alembic migration: `customers`,
  `deployment_tenants`, `fleet_keys`, `heartbeats`, `alerts`,
  `operator_audit` (0010-pattern trigger + hash chain), new
  `control_deployments` columns (defer `desired_configs`, rollups, fleet
  rollouts). `POST /api/fleet/heartbeat` with strict `fleet.v1` Pydantic
  schema, bearer auth against `fleet_keys` (reuse service-key hashing),
  latest-state updates. UI button to mint a fleet key (one-time-envelope
  display).
* **Day 3 — reporter.** Background asyncio task in onebrain-api gated on
  `ONEBRAIN_FLEET_URL`/`ONEBRAIN_FLEET_KEY`; reuse the observability count
  queries; OneBrain-only detail first (skip sibling polling — the public
  `/health` fallback covers modules). Set the two env vars on the shared
  deployment; watch rows arrive.
* **Day 4 — watchdog + alerts + fleet tab.** 1-minute watchdog (missed
  heartbeat → open alert; next heartbeat → resolve), version-drift check on
  ingest, email notifier, cockpit fleet tab from latest-state columns.
  Register MC `/health` with UptimeRobot.
* Deferred within Phase 1: desired-config channel (response is
  `{received:true}` until Phase 3), sibling polling, per-tenant rows,
  rollups + prune, provisioning-workflow fleet-key injection (needed before
  the next new customer, not before Monday).

### Phase 2 — release bundles + upgrade automation (~1 week)

First tasks, in order (onebrain repo for workflows and control-plane wiring):

* `cut-release.yml` (minimal): build onebrain-api/workers/admin-ui +
  communication-api images to GHCR from given refs, capture digests, run
  each repo's existing test suite (compose conformance stack + rollback-
  compat check deferred to before customer #3, matching §1's fleet
  preconditions), POST the manifest with digests to the existing
  `POST /api/operator/releases`.
* `deploy-bundle.yml` + `upgrade-customer.yml`: Railway GraphQL
  image-update + deploy + health-gate loop (copied from
  provision-customer.yml's smoke check) in the section-7 order. Mint one
  Railway project token for the existing project into GitHub Environment
  `dep-<deployment_id>`. Small migration: `kind` column on
  `provisioning_runs`; accept `kind=upgrade` on the existing callback route.
* Wire `control_rollouts` to execution: rollout creation (gates already
  enforced) → `kind=upgrade` run → `GitHubWorkflowDispatcher`; success
  callback → existing `update_rollout_status('success')`. 45-minute
  watchdog (a "mark stale" button is acceptable at this size).
* Run it for real: cut `fleet-v2026.07.1` from main, upgrade Assad's own
  deployment through the existing rollouts tab, verify version bump +
  post-deploy heartbeat, repeat with a trivial change.
* Operator hardening lands here: WebAuthn passkey + TOTP + step-up
  decorator (section 9) before the rollout executor is enabled.
* **Manual ring discipline, no orchestrator:** at 2 deployments, Assad
  clicking rollout per deployment in ring order IS the ring engine.
  `control_fleet_rollouts` + the auto-tick come with the first non-Assad
  deployment.

### Phase 3 — provisioning unification + analytics + alerts (~1–2 weeks)

* Refactor `provision-customer.yml` onto `deploy-bundle.yml` (two-job
  split, project-token minting, fleet-key injection, per-customer LLM key
  step, module datastores) — **before the first external customer**.
* Pre-upgrade Railway backup automation — before the first schema-changing
  rollout to an external customer (until then the manual attestation
  satisfies the `plan_update` gate).
* Desired-config channel (`desired_configs` table + heartbeat response +
  stack-side allowlist apply/ack) and sibling-service polling.
* `heartbeat_rollups` + 90-day prune; analytics page with sparklines and
  cost estimates; alerts inbox + push channel; per-tenant heartbeat rows.
* Recovery-case shell + `kind=recover` dispatch (runbook-driven at first);
  suspend/offboard checklists.
* onebrain-web changes (operator console flag, MC-pointed deploy) land in
  the onebrain repo's `onebrain-web/`; Communication heartbeat inclusion
  (when sibling polling is replaced by richer module detail) lands in
  assaddar-ai-communication; PA health surfaces already exist.

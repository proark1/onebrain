# Hetzner fleet architecture — Mission Control that provisions and steers per-customer Hetzner servers

Status: **design, v2 — hardened after adversarial challenge** (2026-07-11). Decides
how the already-built fleet control plane moves from Railway to Hetzner and how
Mission Control provisions a dedicated Hetzner server per customer and safely keeps
them updated.

Decisions taken (owner):
- **Provisioner:** Mission Control drives the **Hetzner Cloud API** (no external CI),
  behind an isolated broker (§5).
- **Update model:** **pull / self-update** with a **per-customer update policy** and
  **master-controlled targeting** (all / a ring / a named set); a version is only
  offered after **ring promotion** ("safe version").
- **Doc first**, reviewed, before code.

## 0. Challenge outcome — the design is sound, with a mandatory pre-code hardening phase

A 5-dimension adversarial review confirmed **44 issues (6 critical)**. Verdict:
**keep the architecture** — pull-updates, ring promotion, and per-customer isolation
are all defensible — **but the design as first written cannot be built safely**: its
own safety-critical subsystem (updates + convergence) rested on signals the code
does not produce, and two headline claims were false. The fixes are cheap now and
catastrophic to retrofit across a live per-customer fleet. They are integrated below.

**Two structural decisions settled before any P1 code:**

- **D1 — Tenancy is a per-customer TIER, not a fleet-wide law.** Do NOT hardcode
  one-box-per-customer or retire the shared path. Keep a `shared_server` tier
  (dedicated boxes only where GDPR/scale/contract require them; shared tenants
  otherwise). Unit-economics floor of a dedicated box is ~€12–20/customer/mo,
  always-on — dedicated is a premium tier, not the default.
- **D2 — The box VERIFIES, it never TRUSTS Mission Control.** The naive pull model
  *is* remote code execution on a timer: a compromised MC could hand every box an
  attacker image. Closed at the data model (digest pinning + offline-signed release
  manifests + a box-local registry allowlist + downgrade protection — §3b), not
  bolted on later.

**The P1 hardening clusters (all pre-code, most reuse existing primitives):**
A) updates recoverable by construction (expand/contract migrations + `rollback_kind`
+ fence-don't-flap); B) verify-what-you-run (digest + signature + allowlist);
C) observable convergence (real heartbeat truth + a bounded update-outcome field +
the pull-path reconcile driver — this is **new** safety-critical code, not "reused");
D) isolate provisioning authority (broker + offsite snapshots); E) network + secrets
boundary (Cloud Firewall default-deny, no inbound 22, Postgres/Redis internal-only,
block metadata-endpoint egress, bootstrap-token exchange instead of baked secrets);
F) decouple the recovery channel from the app being updated; G) MC disaster recovery
(escrow the token + encryption key) + per-box encrypted backups + an erasure manifest.

---

## 1. Topology

```
Mission Control  (one Hetzner box, EU)
  - OneBrain in ONEBRAIN_OPERATOR_MODE=true  (registry · heartbeat ingest · Fleet console · rollout orchestration)
  - its own Postgres        - Hetzner Cloud API token (provision authority)
  - holds per-customer secrets (one-time-secret envelope)
        │  provisions (Hetzner Cloud API + cloud-init)      ▲ heartbeats + desired-version pull
        ▼                                                   │
Customer box  (one Hetzner box per customer, EU)   ...   Customer box N
  - docker compose: OneBrain + AI Communication + Personal Assistant
                   + Postgres(pgvector) + Redis + Caddy(TLS)
  - host updater (systemd timer) — converges to the desired version
```

Mission Control is the **same OneBrain image** in operator mode. Each customer box
is the **same image set** run as a self-contained stack. Nothing customer-specific
lives in Mission Control except metadata (the registry) and short-lived secrets.

Mission Control itself is **not** self-provisioned — it is stood up once by hand
(`docs/mission-control-standup.md`, adapted for Hetzner). It then provisions
everything else.

---

## 2. Provisioning a customer (direct Hetzner Cloud API)

Replaces the Railway steps in `provision-customer.yml`. The provisioning **run
state machine** (`app/provisioning/runs.py`: `create_run` → dispatch → callback →
one-time bootstrap secret) is **reused unchanged**; only the executor changes from
"dispatch a GitHub workflow that runs `railway ...`" to "call the Hetzner API".

Flow (all inside Mission Control):

1. `POST /api/provisioning/customers` (existing, admin-gated, input-validated) →
   creates the account/spaces/apps/service-keys + registers the deployment
   (`account_id` authoritative) + a `ProvisioningRun`.
2. **New `HetznerProvisioner`** (mirrors `GitHubWorkflowDispatcher`):
   - Mint the box's secrets up front: admin bootstrap password (one-time-secret
     envelope, `OneTimeSecretCipher`), a **fleet enrollment key** (`mint_deployment_fleet_key`),
     and inject the EU-LLM key.
   - Render a **cloud-init** user-data script (template below).
   - `POST https://api.hetzner.cloud/v1/servers` with `{name, server_type,
     location(EU), image(ubuntu), ssh_keys, volumes, user_data}` (bearer =
     `ONEBRAIN_HETZNER_API_TOKEN`).
   - Attach a data **volume** (Postgres data survives a rebuild) and set a **DNS
     record** (Hetzner DNS or Cloudflare API) → `<customer>.fleet.example`.
3. The box boots, runs cloud-init, `docker compose up -d`, Caddy gets a TLS cert.
   The box's OneBrain **self-enrolls** (the three `ONEBRAIN_FLEET_*` vars are in its
   `.env`) → starts heartbeating to Mission Control.
4. **Callback**: cloud-init POSTs the existing provisioning callback
   (`/api/provisioning/runs/{id}/callback`, bearer + key-id, already hardened) with
   the server id, IP, URL, and smoke result → the run is marked succeeded and the
   one-time bootstrap secret is readable once by the operator.

### cloud-init template (sketch)

```yaml
#cloud-config
package_update: true
packages: [docker.io, docker-compose-plugin]
write_files:
  - path: /opt/onebrain/docker-compose.yml   # OneBrain + Communication + PA + Postgres + Redis + Caddy
  - path: /opt/onebrain/.env                  # ONEBRAIN_ACCOUNT_ID, ADMIN_*, ONEBRAIN_FLEET_URL/KEY/DEPLOYMENT_ID,
                                              # ONEBRAIN_VECTOR_STORE=pgvector, sovereign LLM key, ONEBRAIN_DESIRED_VERSION
  - path: /opt/onebrain/update.sh             # the self-update agent (§3)
  - path: /etc/systemd/system/onebrain-update.timer   # runs update.sh every ~5 min
runcmd:
  - cd /opt/onebrain && docker compose pull && docker compose up -d
  - systemctl enable --now onebrain-update.timer
  - <curl the provisioning callback with server metadata + smoke result>
```

Everything interpolated into the script is either operator-controlled config or
server-minted secrets — the same "no untrusted value in a shell/interpreter sink"
discipline the provisioning workflow already enforces applies to the cloud-init
renderer (render via a templating step that quotes/escapes, never string-concat
untrusted input).

---

## 3. Updates — pull / self-update, master-controlled, per-customer policy

The **safety-critical** subsystem. Master decides *what* and *who*; boxes decide
*when* to converge (within their policy); MC never executes on a customer box.

### 3a. "Safe version" = promotion + recoverable-by-construction *(P1-A)*

A release is **promoted**, not just published: `draft` → `internal` → `pilot` →
`early` → `stable`; a ring opens only when the prior ring converged healthy
(existing ring orchestration + `failure_tolerance`). But promotion is *evidence*, not
proof for a different customer's data/scale — so the real safety comes from making a
failed update **recoverable by construction**:

- **Mandate expand/contract (backward-compatible) migrations** for any release that
  can reach an `auto` box. Forbid destructive DDL (drop/rename/retype/not-null-
  without-default) in a single release; split a destructive change across two
  promoted releases (add-new in N; stop-reading-old in N; drop-old in N+1 after N is
  stable). Then the previous image always tolerates the new schema and a **tag
  revert is always data-safe** — no restore, no data loss.
- **Promotion-time migration linter** classifies each release's DDL and stamps
  `ReleaseManifest.rollback_kind ∈ {code_only, restore_required}`; a `restore_required`
  release is **blocked from auto rings** without explicit acknowledgement.
- Recovery branches on that field (see §3e), instead of the naive "revert the tag"
  which does NOT undo a migration and would leave old code on a new schema.

### 3b. Verify, don't trust — the box refuses anything not signed *(P1-B, decision D2)*

The naive pull channel is remote code execution on a timer. Close it at the data model:

- `ReleaseManifest` gains **`images: Dict[module, "registry/repo@sha256:…"]`** —
  digest-pinned, never floating tags. `validate_release` rejects any value without an
  `@sha256:` digest. (Digests also make rollback deterministic and eliminate
  multi-service skew — a box either runs the exact digest set or it doesn't.)
- The manifest is **offline-signed** with a release key MC never holds; each box
  verifies the signature against a public key baked into cloud-init and **refuses any
  digest not in a signed, promoted manifest**.
- A **box-local registry allowlist** (baked into cloud-init) — the box ignores any
  registry the ack names outside it (signing alone would still let a compromised MC
  serve its *own* signed malware from an arbitrary registry).
- **Downgrade protection:** a monotonic version floor + `{deployment_id, nonce, expiry}`
  in the signed desired-state envelope, so a replayed old (CVE-bearing) version is rejected.

### 3c. Per-customer update policy (opt-in / opt-out)

Add an explicit **`update_policy`** to the deployment record (small migration):

| policy | meaning |
|---|---|
| `auto` | follows fleet rollouts for its `release_ring` (opt-in to automatic updates) |
| `manual` | never auto-updated; the master must target it explicitly (opt-out) |
| `pinned` | stays on a specific version until the pin is changed (frozen) |

This subsumes today's `release_ring=manual` convention (which `plan_fleet_rollout`
already excludes) and makes the intent explicit and queryable.

### 3d. Master-controlled targeting

The master creates a fleet rollout targeting **all** eligible deployments, a **ring**,
or a **named set** (specific customers, or an explicit, deliberate update to a
`manual`/`pinned` customer). `plan_fleet_rollout` gains a `policy` filter (`auto`
included in ring sweeps; `manual`/`pinned` only when explicitly named) + an optional
deployment allowlist. **Intra-ring staggering:** `stable` is NOT dispatched as one
wave — sub-canary within the ring so `failure_tolerance` can halt a data/config-
dependent failure before it reaches every stable customer at once.

### 3e. The pull mechanism — host-driven, verify + recover *(P1-A, P1-B, P1-F)*

The **host** `update.sh` (systemd `.timer` → singleton `.service`) is the driver, and
it is **decoupled from the app being updated** (P1-F): it fetches desired-state
*directly* from MC with its own read-scoped token and derives "current" from its own
local state file — so a bad update that kills the app container cannot sever the
recovery path.

1. GET desired-state from MC: `{version, images{module→digest}, migration_to,
   rollback_kind, signature, nonce, expiry}`. **Verify** the signature + registry
   allowlist + downgrade floor (§3b); reject otherwise.
2. If `desired == current` → no-op.
3. **Quiesce + back up** before any schema change: stop the app, `pg_dump` (recording
   the exact pre-migration `alembic current`) client-side-encrypted to off-box storage
   — so the recovery delta is *bounded*, not "every write since some earlier backup".
4. `docker compose pull` (by digest) `&& up -d`; if `migration_from != migration_to`,
   `alembic upgrade head`.
5. **Fence, don't flap:** refuse to start the new app unless `alembic current ==
   migration_to`. On a partial/failed migration do NOT auto-oscillate the tag — hold
   the box `degraded` for the operator (a half-migrated DB must not flip-flop).
6. Smoke-check `/health`. On failure, recover by `rollback_kind`: **`code_only`** →
   revert to the previous digest set (data-safe by §3a); **`restore_required`** →
   restore the §3-step-3 backup into a drop/recreated DB keyed to the recorded
   revision.
7. **Host post-update watchdog:** if local `/health` is unhealthy for M minutes,
   auto-roll-back to the last-known-good digest set *without* MC — plus a
   last-known-good re-pull loop if MC is unreachable.

### 3f. Observable convergence + the pull-path reconcile driver *(P1-C — NEW code)*

The reducer's "safe version" only works if MC can tell **converged vs failed vs dead**.
The current reporter fabricates state (`healthy` hardcoded `True`, `version=""`,
`migration_revision` a compiled constant, `modules=[]`), so this is **new
safety-critical code, not "reused unchanged"**:

- **Report ground truth** (reporter.py rework): real running `version` (build-stamped),
  the *live-DB* alembic revision, real `healthy` (DB reachable + revision == required
  + Redis + deps), and a per-service `ModuleReport` for Communication/PA — so
  convergence is defined over the **full module digest set**, not `onebrain.version`.
- **Bounded update-outcome in the contract** (`fleet.v1 → fleet.v2`, still closed /
  metadata-only): `{last_target_version, outcome ∈ {none,in_progress,succeeded,failed,
  rolled_back}, migration_reached, attempt_id, ts}`. This is the field that lets MC
  distinguish "converged" from "failed-and-rolled-back" from "not-yet-started" —
  which `version + healthy` alone cannot.
- **A periodic reconcile tick** on MC (pull emits no callbacks): it synthesizes each
  deployment's child `RolloutRun` status from the latest `UpdateReport` (gated on
  `attempt_id`), then feeds the **unchanged pure `advance_fleet_rollout`**. A box that
  entered `applying` then went silent past a **per-deployment deadline** synthesizes
  `failed` (counts against `failure_tolerance`); a box never offered the target does
  not. This closes "one wedged box hangs the whole rollout forever" and "a bricked box
  is indistinguishable from an offline box".

**Why pull still wins:** MC needs no inbound execution path to customer boxes; a box
that can't reach MC stays put (fail-safe); with §3e the recovery channel no longer
shares a failure domain with the app. The cost — timer-bounded convergence (minutes)
and this new reconcile/report machinery — is worth it for an unattended fleet.

---

## 4. Reuse vs build

**Reused unchanged:** provisioning run state machine + one-time-secret envelope +
callback auth; the registry, `account_id`, and authorization scoping; fleet keys +
enrollment; heartbeat ingest + overview + history/retention; the watchdog; the
**pure reducer** `advance_fleet_rollout` and the ring model; all security hardening;
the deletion/tombstone contract; RLS.

**New / changed (bigger than first thought — the pull path is new safety-critical code):**
1. `HetznerProvisioner` (Cloud API client + cloud-init renderer + DNS/volume/firewall)
   behind a **broker** (§5) — replaces the Railway executor. A `provisioner` config
   selects Hetzner vs the existing GitHub/Railway path (keep both; Hetzner default).
2. `ReleaseManifest.images` (digest map) + `rollback_kind` + **offline manifest
   signing** + the promotion-time **migration linter** (§3a/§3b). `validate_release`
   enforces `@sha256:` digests.
3. `update_policy` on the deployment record (+ migration); `plan_fleet_rollout` policy/
   allowlist filter + intra-ring staggering.
4. **The pull driver — NEW, safety-critical:** MC computes signed desired-state per
   deployment; a reporter rework that reports *ground truth* + a `fleet.v2` UpdateReport;
   and an MC-side **reconcile tick** with per-deployment deadlines feeding the reducer.
   (The dispatch-a-workflow executor stays for the Railway path; Hetzner uses pull.)
5. **The box side:** host `update.sh` (verify → quiesce/backup → digest-pull → migrate
   → fence → smoke → recover-by-`rollback_kind` → post-update watchdog) + systemd timer
   + the `docker-compose.yml` (all services + Postgres + Redis + Caddy) with a
   layered env-template.
6. Per-box ops baked into cloud-init: Cloud Firewall, Caddy TLS, encrypted off-box
   backups, `unattended-upgrades`, an erasure manifest.

---

## 5. Secrets & security *(P1-D, P1-E)*

**Provisioning authority (P1-D).** The Hetzner API token is a **fleet-wide kill
switch** — Hetzner tokens are read/write and project-scoped only (no per-server or
create-not-destroy granularity; delete-protection toggles with the same token). "Scope
it" is not achievable. So:
- Run provisioning behind a **minimal broker on its own host with its own token**, so
  an RCE in MC's internet-facing operator process (which also ingests heartbeats)
  never touches the raw token. The broker exposes create/DNS but **never a single
  automated un-protect+delete primitive**; rate-limit + out-of-band confirm on destroys.
- **Offsite server *and* volume snapshots** (a `pg_dump` restores only the data plane,
  not destroyed compute/DNS); re-provision-from-snapshot is a tested runbook.
- Optionally **per-ring / per-customer Hetzner projects** so one token can't enumerate/
  destroy the whole fleet.

**No implicit trust of MC (D2).** The pull model does NOT remove MC→box code
execution — MC chooses the image a box runs. §3b (digest pinning + offline signature +
registry allowlist + downgrade floor) is what actually bounds it: a compromised MC
cannot make a box run an unsigned or off-allowlist image.

**Network + secrets boundary (P1-E):**
- **Cloud Firewall, default-deny-inbound, attached in the same `POST /v1/servers` call**
  (never create-then-attach). Allow 80/443 only; **no inbound 22** (break-glass via
  Hetzner console/rescue). In the compose, Postgres and Redis get `expose:` only —
  never `ports:` (Docker's iptables bypasses host ufw); Redis `requirepass`, Postgres
  scram-sha-256.
- **Block egress to `169.254.169.254` from app containers** — the single highest-
  leverage control: that metadata endpoint live-serves the entire user-data blob
  (every baked secret) to any process on the box for its whole life.
- **Bootstrap-token exchange, not baked secrets:** put only a **single-use, short-TTL
  bootstrap token** in user-data (reuse `OneTimeSecretCipher` read-once semantics); the
  box exchanges it once over TLS for its real secrets. The same endpoint, re-fetchable
  via a `secrets_epoch` in the ack, becomes the **rotation channel** the pull model
  otherwise lacks.
- **Per-box LLM key** (never one shared fleet-wide credential), routed through a local
  egress broker so the raw key is never in the app's `os.environ`.
- SSH: an ops key for break-glass only; the normal update path needs no inbound SSH.

---

## 6. Ops non-negotiables (the Hetzner tax) *(P1-G)*

On Railway these were managed; on Hetzner the provisioner bakes them in:
- **Mission Control disaster recovery.** MC is a SPOF for the whole fleet's *control*
  (not its data): it holds the token, the registry, and the `ONEBRAIN_SECRET_ENCRYPTION_KEY`
  that a Postgres restore cannot reconstruct. Off-box PITR backup of MC's Postgres +
  **escrow of the Hetzner token and the encryption key** (a DB restore is useless for
  decrypting envelopes without the key). On MC-unreachable, boxes **fail-safe to hold**
  (short TTL on the cached desired-state; MC re-asserts `apply:true` each ack).
- **Per-box backups:** client-side **encrypted** (keys escrowed to MC/KMS), pinned to a
  named EU region, **WAL/PITR** (not nightly-dump-only) with a stated RPO/RTO; report
  backup *freshness* in the heartbeat and degrade readiness on stale backups. Restores
  are drilled periodically — unproven backups aren't backups.
- **TLS**: Caddy auto-provisions + renews. **OS patching**: `unattended-upgrades`.
- **Monitoring**: MC's missed-heartbeat watchdog covers app liveness; add an external
  dead-man ping on MC's own `/health` + disk/CPU alerts per box (disk-full during a
  migration or backup is a real failure mode — pre-flight gate it).
- **Teardown / erasure:** bind an **erasure manifest** to the deployment at provision
  time (server / volume / snapshot / backup-prefix / user-data / DNS / secret ids);
  teardown **iterates and verifies** deletion (Hetzner volumes survive a server-destroy).
  A GDPR erasure **suppresses the snapshot step** and **crypto-shreds** backups via the
  per-box key (rather than editing dump blobs); honour the existing legal-hold gate.

---

## 7. GDPR posture

EU Hetzner boxes + a Hetzner DPA give data **residency** — necessary, not sufficient.
Residency + EU-sovereign LLM routing (`sovereign_llm_model` / `sovereign_required`,
flip `pii_phase=dpia_signed` once wired) + the already-built consent / retention /
erasure / audit controls make a genuinely defensible posture, materially stronger than
US-centric managed infra. **Do not over-claim from residency** — still required and NOT
covered by "EU box": a maintained **sub-processor list** (Hetzner + the LLM vendor + any
DNS/backup provider), the **DPA chain** to each, an Art. 30 **record of processing**, an
Art. 35 **DPIA**, and treating **cloud-init secret-at-rest** (§5) as a processing risk
in that DPIA. The LLM endpoint is the usual data-leaves-the-EU leak; it is now a config
lever, not a rewrite.

---

## 8. Migration path from the current Railway/GitHub-Actions provisioning

1. Keep the GitHub/Railway provisioner working; add `HetznerProvisioner` behind a
   `provisioner` config switch (no regression to existing behaviour).
2. Stand up Mission Control on Hetzner (per the standup runbook, adapted).
3. Provision a **scratch** customer box end-to-end (dry-run then real) and verify:
   API creates the server, cloud-init brings the stack up, TLS, self-enroll,
   heartbeats land, the callback completes, the update timer converges a version.
4. Move `internal` (your own) boxes first; then pilot; migrate customers by
   re-provisioning + data restore, or a lift path, per customer.
5. Retire the Railway path once the fleet is on Hetzner.

---

## 9. Build phases (revised — hardening is P1, not deferred)

- **P0 — data model + trust (do first, pure/unit-testable):** `ReleaseManifest.images`
  (digest map) + `rollback_kind` + `validate_release` digest enforcement; offline
  manifest signing + a box-verify library; `update_policy` on the deployment; the
  `fleet.v2` UpdateReport contract + reporter ground-truth rework. All unit-testable
  with no Hetzner.
- **P1 — provisioner core (behind the broker):** `HetznerProvisioner` (Cloud API client
  with injectable opener, cloud-init renderer with the firewall + bootstrap-token
  exchange + metadata-egress block, the `docker-compose.yml` with internal-only
  Postgres/Redis), wired into the provisioning run/callback path; unit-tested with a
  fake API client + rendered-template assertions. The real API call is the infra tail.
- **P2 — pull orchestration:** MC computes **signed** desired-state per deployment;
  `plan_fleet_rollout` policy/allowlist filter + intra-ring staggering; the MC-side
  **reconcile tick** with per-deployment deadlines feeding the pure reducer. Unit-tested.
- **P3 — the box side:** host `update.sh` (verify → quiesce/backup → digest-pull →
  migrate → fence → smoke → recover-by-`rollback_kind` → post-update watchdog) + systemd
  timer; the promotion-time migration linter. Verified on a scratch box.
- **P4 — ops/DR:** MC DR + escrow; per-box encrypted PITR backups + restore drill;
  erasure manifest + teardown; monitoring.

---

## 10. Decisions

**Settled:** provisioner = Hetzner API behind a broker (§5); update model = pull with
per-customer policy + signed desired-state (§3); D1 tenancy tiering; D2 verify-don't-trust.

**Still to settle before P0/P1:**
1. **Tenancy tiers (D1 detail)** — add a `shared_server` tier (the enum slot exists);
   which customers get dedicated boxes vs shared; the €/customer floor for dedicated.
2. **DNS provider** — Hetzner DNS vs Cloudflare (Cloudflare = easier API + proxy/WAF).
3. **Server sizing & data volume** per customer; vertical-scale vs bigger box.
4. **Signing infrastructure** — where the release private key lives (HSM/KMS, never MC);
   cosign vs Notation; key rotation.
5. **Backup engine** — WAL/PITR tooling (pgBackRest / wal-g) + off-box store + region pin.
6. **Fleet security floor** — a `manual`/`pinned` customer sitting on a known-CVE version:
   does a critical security release override opt-out (with notice)? (deferred, but decide
   the policy).

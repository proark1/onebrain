# Hetzner Migration Sequence — Hetzner First, Merge Last

Status: **approved plan, not started** · Date: 2026-07-12
Companion to: [hetzner-fleet-architecture.md](hetzner-fleet-architecture.md) (the target design this plan ships)

This document fixes the *order* of the Railway→Hetzner migration and the codebase
restructuring. It is the output of an adversarial sequencing gate (workflow
`wf_56f1f7ed-498`): 3 scout agents extracted the real deploy/CI coupling from the
`onebrain`, `assaddar-ai-communication`, and `personalasisstant` repos; 4 adversarial
reviewers (production-safety, sequencing, git-surgery, CI/build lenses) attacked the
draft plan; a synthesizer folded the results. **All four reviewers returned
`must_change` — 34 issues, 7 critical — and the draft ordering was overturned.**

---

## 0. Verdict: restructure-first is overturned — Hetzner-first, merge-last

The draft plan proposed merging the three repos into a monorepo *first*, then building
the Hetzner stack inside it. Both stated justifications failed on evidence:

1. **The Hetzner deploy layer is repo-agnostic.** It consumes GHCR image digests plus a
   compose file. `provision-customer.yml` already proves cross-repo image consumption
   today via the `ASSISTANT_SERVICE_IMAGE` / `COMMUNICATION_*_IMAGE` secrets. "Built
   once in its final home" buys nothing.
2. **The CI math is lopsided.** Per-repo digest CI is ~50 lines appended to two
   already-green pipelines. Monorepo CI requires rewriting every workflow, plus
   `tests.yml`'s secret-scan job (which walks `Path('.')` with a hardcoded allowlist)
   fails closed on day one of the merged layout.
3. **Restructure-first forks production.** A multi-week window with two mains and
   divergent alembic/SQL migration lineages, across **four** live production surfaces
   (see §1), policed by a solo operator.

New order: development continues in the existing repos, which stay the live deploy
source throughout. The merge happens **after** cutover, off the critical path, when the
two Railway workflows (`provision-customer.yml`, `update-customer.yml`, ~340 lines of
Railway CLI) can be **deleted instead of ported**.

## 1. Production surface inventory (corrected)

The draft assumed two repos and three services. Reality:

| Surface | Repo | Services | Deploy path |
|---|---|---|---|
| OneBrain | `onebrain` | onebrain-api, onebrain-workers, onebrain-admin-ui (+ Postgres/pgvector) | Railway auto-deploy on push to main |
| AI Communication | `assaddar-ai-communication` | api, workers, voice, admin, widget (one shared image) (+ Postgres, Redis) | Railway |
| Personal Assistant | `personalasisstant` (`onebrain-assistant`) | assistant-api, assistant-worker (one image), assistant-web (+ Postgres, Redis) | Railway, via a `railway.json` template-swap PowerShell script |
| Voice edge | `assaddar-ai-communication` | Go binary | **Existing Hetzner box**, git-pull deploy |

Corrections the gate forced:

- **`personalasisstant` is a live third production system** (deployed 2026-07-10), not
  a leftover. `onebrain/app/assistant/` is **contracts-only**; the runtime is NOT in
  onebrain. PA talks to OneBrain over `api/service/*` with a service key
  (`ONEBRAIN_API_BASE_URL` hardcodes the Railway host today). Its `assistant.v1`
  contract is graduating to enforced handshake-rejection on version mismatch.
- **A Hetzner box already exists in production** (voice edge). It is the fourth
  surface and must not go silently stale during the migration.

## 2. Phases

### Phase 1 — Preflight branch and dependency triage (existing repos, Railway live)

- Merge `docs/hetzner-fleet-architecture` into onebrain `main` NOW (docs-only,
  auto-deploy harmless). Phases 3–4 are built from this doc and it currently lives
  only on an unmerged branch that any snapshot/merge would strand.
- Triage `feature/intake-retrieval-projection` (onebrain); triage
  `feat/operator-human-takeover` and close dependabot branches (comm).
- Inventory `personalasisstant` as a live third production system and make the scope
  decision (§5, decision 1). Note it has uncommitted local work.
- Dev-box hygiene while free: enable Windows long paths + `git config core.longpaths`
  (needed by the Phase 8 merge).

### Phase 2 — Registry image CI per repo + image-portability fixes

- onebrain: GHCR build+push workflow with digest outputs for the 3 images
  (`Dockerfile`, `Dockerfile.worker`, `onebrain-web/Dockerfile`).
- comm: same for its single shared image via the existing `ci.yml`.
- personalasisstant: minimal GHCR-publish workflow so `ASSISTANT_SERVICE_IMAGE` is
  digest-pinnable.
- **Prerequisite fixes for digest-pinned signed images**, done here:
  - **(a) Kill the `NEXT_PUBLIC_*` build-time bake** in comm's admin bundle and PA's
    web app — move to runtime-injected config or same-origin relative paths behind the
    box reverse proxy. One signed digest must serve every box; per-customer rebuilds
    defeat signing. Today's images silently call `assaddar-api-production.up.railway.app`
    until Railway dies.
  - **(b) Fix comm's shared `HEALTHCHECK`** (currently curls the API `:4000/health`
    for all five services) — per-service `HEALTHCHECK_URL` plus liveness
    endpoints/process probes for workers and admin. Otherwise the Phase 3 ground-truth
    reporter reads false-unhealthy and Phase 4's `update.sh` recover path loops on
    permanently-"unhealthy" containers.
- Railway keeps deploying from these repos untouched.

### Phase 3 — Hetzner P0 trust primitives in the existing onebrain repo

Lands on onebrain `main`, auto-deploys to Railway, and is exercised by the live fleet
control plane immediately:

- Image digests/signing, `rollback_kind`, `update_policy`, fleet.v2 `UpdateReport`,
  real ground-truth reporter (per the architecture doc).
- `rollback_kind` classification extended to **comm's hand-written SQL migrations**
  (no down migrations, duplicate `0010` filenames) — not just alembic.
- Decide NOW what the Hetzner provisioner writes into
  `railway_project_id`/`railway_environment_id` (box id / compose project) so
  `resolve_railway_target()` works without an emergency schema migration.
- Pull forward only the **thin contract slices** Hetzner needs (full contracts
  extraction is Phase 9):
  - fleet.v2 models;
  - a **per-module env-var manifest** the provisioner must satisfy — including
    `ONEBRAIN_SERVICE_KEY` + `ONEBRAIN_SPACE_ID` delivery, which
    `provision-customer.yml` never set, with a boot-time check so comm can't silently
    run in local-brain mode;
  - a **per-module health-probe manifest** (module_id → probe type/port/path) consumed
    by the ground-truth reporter.

### Phase 4 — Hetzner P1–P3: provisioner, pull orchestration, box-side update.sh

- HetznerProvisioner behind an isolated broker; signed desired-state + MC reconcile
  tick; box-side `update.sh` (verify + recover). Authored under `deploy/` in the
  onebrain repo — it is repo-agnostic (consumes GHCR digests) and moves with a
  `git mv` at the Phase 8 merge.
- Compose rules:
  - Author from the Dockerfiles' own defaults (8000/3000/4000/4100/5174), **never**
    from `provision-customer.yml`'s Railway-masked `:8080` wiring.
  - Run each stack's migrations as **one-shot services** (alembic; `pnpm db:migrate` —
    the advisory lock serializes) gated by
    `depends_on: service_completed_successfully`, so comm workers/voice/admin don't
    race an empty DB.
  - Strictly **per-service env files** — both codebases read the `ONEBRAIN_` prefix
    with different semantics.
  - `/data` bind mount for `ONEBRAIN_DATA_DIR`; `TRUST_PROXY` set for the box's actual
    proxy hop count.
- Optionally fold the voice-edge Go binary deploy into this pipeline, retiring the
  git-pull-on-box mechanism.

### Phase 5 — E0: Mission Control bootstrap on Hetzner + observability flip

- MC cannot provision its own first box. Bootstrap the MC Hetzner box by **manually
  invoking the SAME `deploy/` cloud-init + compose artifacts and signed images** (not
  an ad-hoc hand-build), then enroll it in its own `update.sh` pull path so it is
  fleet-managed thereafter. Only the very first invocation is manual.
- Generate signing/enrollment keys. MC DB: start fresh vs migrate control-plane tables
  from the Railway onebrain Postgres (§5, decision 3).
- Flip as **one change window**: `ONEBRAIN_FLEET_URL` / `ONEBRAIN_FLEET_PUBLIC_URL`
  and `ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS` on every deployment, off
  `*.up.railway.app`. Verify a heartbeat **arrives** at the new MC from each stack;
  treat no-heartbeat-within-N-minutes as a rollback trigger, never silence-as-success
  (the reporter fails quietly by design).

### Phase 6 — Dogfood migration of nft_gym + assaddar_ai_communication

Provisioning creates **empty** stacks — data migration is a separate, checklisted
step. Per stack:

1. Provision the box via the real path; write freeze.
2. `pg_dump`/`pg_restore` both Railway Postgres DBs with pgvector extension-version
   parity.
3. Copy the `/data` volume (the app boots fine without it — loss is silent).
4. Preserve tenant slugs, account ids, and `source_ref`s **VERBATIM** — they are the
   GDPR erasure join keys on both sides; renaming strands records from deletion or
   maps tombstones to the wrong tenant. Verify row counts + a sample of `source_ref`s
   resolve identically + tombstone cursor intact.
5. On the new Postgres run `create-app-role.sql` + `enable-force-rls.sql` and
   `pnpm db:check` with `REQUIRE_DB_RLS=true` (otherwise RLS is silently inert).
6. Set `ADMIN_PUBLIC_URL` / `VOICE_PUBLIC_URL` / `API_PUBLIC_URL` and runtime client
   config explicitly (fallbacks are Railway domains).
7. Deliver service keys + space ids per the env manifest; verify comm is actually
   talking to onebrain, not local-brain fallback.
8. Repoint personalasisstant's `ONEBRAIN_API_BASE_URL` (and rebake its
   `NEXT_PUBLIC_ASSISTANT_API_URL`) before proceeding.

### Phase 7 — Railway-off rehearsal, then decommission

- **Before deleting anything:** block egress to `*.up.railway.app` on every Hetzner
  box (or equivalent) and run the full stack — this flushes every baked/fallback
  Railway URL that "works" only because Railway is still up.
- Add a CI grep-audit that fails on `*.up.railway.app` inside built images.
- Widget embeds: tenant sites embedding
  `assaddar-widget-production.up.railway.app/widget.js` break silently at
  decommission — regenerate embed snippets or keep a redirect through a deprecation
  window (§5, decision 4).
- Then decommission the Railway projects (PA's services included/excluded per the
  Phase 1 decision). Warm-standby duration: §5, decision 5.
- **Do NOT archive the old repos** — they still drive Hetzner via GHCR CI.

### Phase 8 — Monorepo merge (post-cutover, off the critical path)

Mechanics per the git-surgery findings:

- First commit of the merged history is `.gitattributes` (`* text=auto`, `eol=lf`
  pinned for `*.sh`/`deploy/**`/cloud-init). Both indexes are all-LF today, so this is
  a no-op renormalization; retrofitting later conflicts with every unported branch,
  and `autocrlf=true` would otherwise ship CRLF `update.sh` to boxes.
- Use neither subtree nor raw filter-repo (not installed; rewrites SHAs, breaking
  later branch ports): in each source repo create a prep branch with a single
  `git mv` commit to the final path (`services/onebrain` + `web/console`;
  `services/communication` **verbatim** — `pnpm-lock.yaml` byte-identical,
  `apps/widget` stays put since it dies as a service once the proxy serves `widget.js`
  static), then `merge --allow-unrelated-histories`; fetch comm with `--no-tags`.
- **Delete** `provision-customer.yml`/`update-customer.yml` instead of porting;
  rewrite `tests.yml`'s secret scan with monorepo-aware paths or replace with
  gitleaks; migrate GitHub secrets by checklist; re-root husky (`core.hooksPath`) and
  verify a hook fires; fix comm's five dotenv `../../../.env` resolutions; add a
  minimal pyproject or enforce per-directory invocation for
  app/onebrain_sdk/alembic/pytest; fix onebrain-web's `openapi` script path; repoint
  the voice-edge box's clone remote+path if not already folded into the box pipeline
  in Phase 4.
- Only after everything is repointed, archive the old repos read-only.

### Phase 9 — Contracts extraction + vocabulary reconciliation

- The runtime surface is small (one client class, 6 endpoints, one auth scheme);
  nothing in the Hetzner path depended on it — which is why it waits.
- The real work is a design decision first: reconcile the **three divergent
  record_type/intent vocabularies** (`app/intake/base.py` vs
  `app/assistant/contracts.py` vs comm's hand-mirrored TS enums) and fix
  `/api/service/capabilities`, which advertises the assistant vocabulary while intake
  validates against a different set. Extracting first would freeze that inconsistency
  into a versioned package. (§5, decision 6.)
- Then: single schema source with generated types **committed into each service tree**
  (`services/onebrain/app/contracts_gen/`, `packages/contracts/`) plus a
  regenerate-and-fail-on-diff CI job — per-service Docker build contexts survive
  unchanged and path filters keep working (`contracts/**` added to every service's
  filter). Pin the `source_ref` grammar; fold in or delete `onebrain_sdk` (a third
  hand-written copy); add a cross-repo conformance test (intake silently
  keyword-reclassifies unknown types today, so drift is invisible at runtime).

## 3. Rejected challenges (and why)

- **Dual-landing discipline / drift-diff CI between two mains** — right diagnosis,
  wrong remedy: the reordering eliminates the fork instead of policing it.
- **Branch-protect old mains, disable auto-deploy during the window** — mooted; with
  Hetzner-first the old repos remain the single dev+prod line (today's status quo).
- **"New-repo secrets needed from day one"** — GHCR pushes use the built-in
  `GITHUB_TOKEN` with `packages:write`; the full secrets inventory only becomes due at
  the Phase 8 merge checklist.
- **"Freeze-with-hotfix-protocol OR repoint Railway at the monorepo immediately"** —
  both horns rejected; don't fork at all (merge last) dominates both.
- **"New repo, reject in-place" as a hard constraint** — downgraded; post-cutover,
  deploys are pull-gated by signed manifests, so a bad layout commit just fails CI.
  New-repo vs in-place becomes a preference (§5, decision 2).
- **"Accept that MC itself is not dogfooded"** — rejected in favor of the stronger
  bootstrap: MC is built from the same deploy artifacts and then self-managed.
- **"Repoint Railway Root Directory at the monorepo right after the merge"** —
  rejected; trades the fork for an early prod-touching atomic flip solely to preserve
  a merge ordering the evidence no longer supports.

## 4. Open decisions

1. **personalasisstant scope:** merge into the eventual monorepo as
   `services/assistant` (it has uncommitted local work and 3 Railway services of its
   own) vs keep it a separate repo with its own GHCR digest pipeline + repoint tasks.
2. **Phase 8 shape:** still worth doing at all — and if yes, NEW repo
   (`onebrain-platform`, clean but full secrets re-creation) vs restructure onebrain
   in place (viable once deploys are pull-gated; keeps repo identity/issues/tokens).
3. **MC database at bootstrap:** start empty (defensible with zero external customers;
   loses provisioning-run/audit history, requires re-enrolling both stacks) vs migrate
   control-plane tables out of the Railway onebrain Postgres.
4. **Widget embed cutover:** update embed snippets on every tenant site at cutover vs
   keep the Railway widget domain as a redirect through a deprecation window.
5. **Railway decommission timing:** hard-off at Phase 7 vs warm standby for N weeks as
   a rollback path.
6. **Vocabulary reconciliation direction (Phase 9):** converge on ONE canonical
   record_type/intent vocabulary vs explicitly namespacing `intake.v1` and
   `assistant.v1` — a product taxonomy decision, not an engineering default.

## 5. Gate traceability

- Workflow `wf_56f1f7ed-498`, 2026-07-12: 3 scouts (onebrain coupling, comm coupling,
  cross-service contract surface) + 1 standalone PA-repo scout + 4 adversarial
  reviewers + 1 synthesizer. 8/8 agents completed.
- Verdicts: prod-safety `must_change` (2 critical), sequencing `must_change`
  (3 critical), git-surgery `must_change` (1 critical), ci-build `must_change`
  (1 critical). 34 issues total.
- Every accepted issue is folded into the phases above; rejections are listed in §3
  with reasons. Full agent transcripts live in the session workflow journal.

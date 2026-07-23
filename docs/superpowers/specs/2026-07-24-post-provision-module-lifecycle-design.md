# Post-provision module lifecycle — design

**Status:** design agreed (O1a + O2b). **Phase 1 backend implemented** — the bundle-delivered
descriptor, `set_deployment_modules` re-mint, and the `POST /deployments/{id}/product-modules`
operator endpoint (DB-only, add-only). P2–P4 and the console UI are still to come.
**Scope requested:** activate *and* deactivate any product module (DB-only **and**
service-backed) on an already-provisioned deployment, from Mission Control, without
re-provisioning the box or losing customer data.

## 1. Problem

Product modules are installed **once, at provisioning time**. The operator selects
optional `module_ids`; `resolve_module_composition`
([bundles.py](../../../app/provisioning/bundles.py)) turns them into spaces + app
installations + container services; the non-secret descriptor is baked into `box.env`
as `ONEBRAIN_CUSTOMER_BOOTSTRAP`; and on every boot the box runs
`reconcile_customer_bootstrap` ([main.py:224](../../../app/main.py)) to converge its
own platform DB. After that, **there is no supported way to change a deployment's
module set.** A customer who buys Accounting three months later, or a dev gate that
was provisioned before a module existed, is stuck — every panel for the absent module
renders its "not enabled" empty state (the per-workspace install gate,
[accounting.py:211](../../../app/routers/accounting.py)).

The current escape hatch — *replace the box* — is acceptable for the stateless dev
gate (this is what the `mc-gate-auto-replace` work does) but **not** for a customer:
replacement means data migration and downtime.

## 2. What already exists (and is reusable)

- **The box already reconciles on every boot** and the reconcile is idempotent /
  upsert-only ([customer_bootstrap.py:233](../../../app/provisioning/customer_bootstrap.py)).
  Change the descriptor's `module_ids` and restart `onebrain-api`, and new spaces/app
  installations appear with **zero new box-side code** — for DB-only modules.
- **Integration keys already ride a live channel.** The sealed secret bundle
  (`BUNDLE_KEYS`, [bootstrap_bundle.py:24](../../../app/fleet/bootstrap_bundle.py))
  carries `ONEBRAIN_ASSISTANT_SERVICE_KEY` / `ONEBRAIN_COMMUNICATION_SERVICE_KEY`
  / …, and the gate agent **re-fetches the bundle every update cycle**
  (`update.sh` step 0, the `UPDATE_BUNDLE_REFRESH_FAILED` guard,
  [update.sh:718](../../../deploy/box/update.sh)).
- **The release pins all module images fleet-wide**; a box pulls only the modules named
  in `UPDATE_LOCAL_MODULES` (`update.sh` `select_images`). So the *images* for a
  not-yet-enabled module are already reachable from any current release.
- **`selected_module_ids` is already persisted on the deployment**
  ([provisioning.py](../../../app/routers/provisioning.py)) and surfaced in the gate
  diagnostic ([operator.py:2327](../../../app/routers/operator.py)).

## 3. What does NOT exist — the delivery gap

| Channel | Carries | Live-updatable on a running box? |
|---|---|---|
| Signed desired-state `/api/fleet/desired-state` | release/images only; envelope is Phase-3-frozen, `extra="forbid"` ([desired_state.py](../../../app/controlplane/desired_state.py)) | yes, but **cannot carry topology** |
| Sealed secret bundle (`.env`) | secrets incl. integration keys | **yes** (re-fetched each cycle) |
| `box.env` + `docker-compose.yml` + `env/*.env` | the descriptor, `UPDATE_LOCAL_MODULES`/`UPDATE_PROFILES`, **compose service definitions** | **no** — written once by cloud-init, never re-delivered |

Two hard consequences:

1. **The module descriptor lives in the frozen channel.** `ONEBRAIN_CUSTOMER_BOOTSTRAP`
   is a `box.env` value ([render.py:815](../../../app/provisioning/hetzner/render.py)),
   so today it cannot change on a live box.
2. **The compose only contains the *enabled* modules' services.** `render_compose`
   iterates `inp.enabled_modules` ([render.py:499](../../../app/provisioning/hetzner/render.py)),
   and `update.sh` swaps only the image-override, never the base compose. So a
   service-backed module's containers are **absent from the box entirely** until the
   compose is re-rendered and re-delivered — a capability that does not exist.

## 4. Two tiers × two directions

`modules=()` in the catalogue marks a module that runs **inside** the Core containers.

| Module | Kind | Adds containers? | Adds secrets? |
|---|---|---|---|
| `kpi_dashboard`, `ai_employees`, `buchhaltung` | **DB-only** | no | no |
| `assistant` | service-backed | `assistant-service` | assistant key |
| `communication` | service-backed | 4 comm containers | comm key + space id |

- **Tier-1 activate** = descriptor delta + `onebrain-api` restart → reconcile upserts.
  No compose change, no new container, no new secret.
- **Tier-2 activate** = Tier-1 **plus** re-rendered compose + new `env/*.env` + new
  integration keys + `UPDATE_LOCAL_MODULES`/`UPDATE_PROFILES` flip + image pull + `up`.
- **Deactivate** (either tier) = destructive: remove app installations; for service
  modules also stop/remove containers, revoke keys, drop the module's DB/role; and
  decide the fate of the module's customer **data** (Drive files in a removed space,
  accounting documents, …) — a GDPR-relevant teardown, not a config flip.

## 5. Design

### 5.1 Invariants held

- Config reaches a box **only** through the two sanctioned channels (bundle / signed
  desired-state). No SSH, no ad-hoc push. (AGENTS.md: boxes have no SSH.)
- Every rendered `box.env` value stays shell-safe (`_shell_kv`); the descriptor is
  url-safe base64 already.
- Operator-initiated only; never auto-applied to customers. No change to release
  signing / customer approval.
- Additive reconcile stays additive; **removal is a separate, explicitly-authorized
  path** with its own audit + data-handling decision.

### 5.2 Mission Control side (all phases)

- New operator endpoint `PATCH /api/operator/deployments/{id}/modules` (add/remove a
  set), guarded like the other operator mutations; writes an audit event and updates
  `selected_module_ids`.
- A guardrail layer: reject unknown ids (`resolve_module_composition` already does);
  reject a remove that would orphan Core; require an explicit `confirm_data_loss` flag
  for any remove that drops a space holding data.
- Console UI: a module toggle list on the deployment detail page (add = one click;
  remove = a typed-confirm wizard, mirroring the Decommission wizard).

### 5.3 The topology-delivery channel — RESOLVED: ride the bundle (O1a)

Make the **module selection a re-fetched input**, not a frozen one, by reusing the
**existing sealed-bundle exchange** (`POST /api/fleet/bootstrap`) rather than a new
signed sibling to desired-state.

Concrete mechanics:

- Today `env/onebrain-api.env` bakes `ONEBRAIN_CUSTOMER_BOOTSTRAP` and
  `ONEBRAIN_LOCAL_MODULES` as **literals** ([render.py:807/815](../../../app/provisioning/hetzner/render.py)),
  while the integration keys next to them are `${VAR}` **refs** compose interpolates
  from the re-fetched `.env`. **Change the two topology values to `${VAR}` refs too** —
  the exact pattern the integration keys already prove works.
- Serve them from the bundle. Because the descriptor is **non-secret**, MC can **overlay
  the current `selected_module_ids`-derived descriptor + `LOCAL_MODULES` at bundle-serve
  time** — the same "inject current state without re-encrypting" move the wrapper-key set
  already uses ([fleet.py:269](../../../app/routers/fleet.py)). A DB-only change then needs
  **no re-mint at all**; a service-module change re-mints only to add the new integration
  key (epoch bump), keeping key + descriptor **atomic in one epoch**.
- The gate agent already re-fetches the bundle each cycle; a `docker compose up -d`
  re-interpolates the api env and restarts `onebrain-api`, whose boot reconcile upserts.

Why not a new signed desired-state sibling (O1b): the desired-state envelope is
Phase-3-frozen (`extra="forbid"`) and the app-free box verifier is pinned byte-for-byte
to `app.trust.envelope` — topology can't extend it, so O1b is net-new signing + verify +
conformance surface. The bundle is already authenticated, encrypted, rate-limited,
epoch-versioned, and **must change anyway** to carry a service module's key; putting
topology there keeps the two changes on one atomic channel.

### 5.4 Box side

- **Tier-1 apply:** write the new descriptor, `docker compose up -d` (or restart
  `onebrain-api`); the existing boot reconcile upserts the new installations. Add a
  bounded health-check + report, reusing `update.sh`'s recover/rollback discipline.
- **Tier-2 apply — RESOLVED: full superset skeleton at provision (O2b).** Render the box
  as a **complete superset**: compose over all `PRODUCTS` (profile-gated), **all** module
  `env/*.env`, **all** product DBs + roles, and Caddy routes for every module — then gate
  purely at runtime via `UPDATE_PROFILES` / `UPDATE_LOCAL_MODULES`. Tier-2 activation
  becomes the **same shape as Tier-1**: bundle serves the new topology + re-minted key,
  the agent flips the profile, pulls the module's images (already release-pinned
  fleet-wide), and `up -d` — which naturally runs that product's profile-gated migrate
  against its already-present DB. **No host asset is ever delivered to a live box.**
  - Idle cost is negligible: empty DBs/roles, profile-down (never-started) service defs,
    Caddy routes that 502 until their upstream runs (Caddy retries dead upstreams — it
    never blocks on a down profile). **No idle containers.**
  - Invariants intact: the control-plane Caddy deny-list and `is_operator_surface` router
    gate are independent of module routing, so a superset customer box still cannot reach
    `/api/fleet|operator|provisioning|rollouts`.
  - The rejected option (a) — re-render + deliver compose/env/caddy to a *live* box — would
    require a new verified host-asset swap+rollback channel; a bad compose bricks a box
    that has **no SSH recovery**. O2b keeps that bricking-prone mutation at provision time,
    where a bad render just fails a *new* box.
- **Deactivate apply:** `compose stop/rm` the removed profile's services; run the
  removal reconcile (new box-side code) that deletes app installations / keys and
  performs the chosen data disposition; report.

### 5.5 Deactivation data model (highest risk)

`reconcile_customer_bootstrap` is upsert-only by design. Deactivation needs a **new,
separate** `deprovision_module` path with an explicit disposition per removed space:

- **retain** (default): keep spaces + data, only remove the app installation +
  purposes so the module UI goes dark but nothing is destroyed (fully reversible).
- **purge**: after an operator typed-confirm, delete the module's data (Drive subtree,
  accounting rows), revoke keys, drop the service DB/role. Irreversible; emits a GDPR
  audit record. Consider an export-first step.

Ship **retain-only first**; purge is its own phase behind the confirm flag.

## 6. Phased plan

| Phase | Deliverable | Risk |
|---|---|---|
| **P1** | Topology-delivery channel + Tier-1 **activate** (kpi/ai_employees/buchhaltung). MC endpoint + audit; signed re-fetched topology doc; box applies + reconciles + reports. | med — new channel, but additive & non-destructive |
| **P2** | Tier-2 **activate** (assistant/communication) via the recommended full-compose-profile-flip; integration-key minting through the bundle; image pull + profile `up` + health/rollback. | high — touches compose render + service lifecycle |
| **P3** | **Deactivate — retain** (both tiers): module goes dark, data untouched, reversible. | med |
| **P4** | **Deactivate — purge**: destructive teardown + GDPR export/audit; drop DB/role. | high — irreversible, GDPR |

Each phase is independently shippable and leaves the fleet in a valid state. P1 alone
already solves the common "customer bought a DB-only module later" case and unblocks a
mis-seeded dev gate without replacement.

## 7. Decisions

- **O1 — RESOLVED: ride the bundle (§5.3).** Topology travels as `${VAR}` refs served by
  the existing sealed-bundle exchange, overlaid from `selected_module_ids` at serve time;
  a service-module change re-mints only to add its key (atomic epoch). Rejected the new
  signed desired-state sibling: the envelope is frozen and the box verifier is byte-pinned.
- **O2 — RESOLVED: full superset skeleton at provision (§5.4).** Render every box with all
  services (profile-gated), env files, DBs/roles, and Caddy routes; gate at runtime.
  Activation never ships a host asset to a live box. **Now is the ideal time: the fleet has
  no customer boxes yet (MC + gates only), so there is zero migration cost, and the gate is
  replaced anyway.** The one real cost is that this changes the provisioning render for all
  boxes — a provisioning-contract change that P2 must land and test carefully (role-split
  preflight, postgres-init over all roles, migrate services staying profile-gated).
- **O3** — apply trigger: does `onebrain-api` self-reconcile on the next boot after the
  topology changes, or does the gate agent own the `up -d`? (Leaning: agent-owned, so a
  bad apply is recoverable outside the app container, like `update.sh`.) *Still open —
  settle at the start of P1.*
- **O4** — purge default & export-first requirement for GDPR. *Open; only bites at P4.*

## 8. Test surface

- Unit: MC endpoint guardrails (unknown id, Core-orphan, confirm-flag); topology doc
  sign/verify; reconcile add + remove(retain) + remove(purge).
- Box script: extend `tests/test_box_update_sh.py` for the topology fetch/verify/apply
  and rollback (shellcheck is CI-only — mind `deploy/box/*.sh`).
- Contract: regen both OpenAPI surfaces (new operator route).
- Migration: none expected for P1; P4 (drop role/db) may need one — bump
  `REQUIRED_ALEMBIC_REVISION` if so.

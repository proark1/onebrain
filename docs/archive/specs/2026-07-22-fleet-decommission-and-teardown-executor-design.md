# OneBrain fleet decommission & teardown executor

```
status:     draft v1 (implementable spec) — NOT yet built
date:       2026-07-22
owner:      OneBrain / Mission Control
supersedes: hetzner-fleet-architecture.md §"Destructive lifecycle" (on landing)
scope:      Mission Control fleet view (mc.onlyonebrain.com) — operator_mode only
```

> **Placement note.** This is an **active, forward-looking spec**, not a historical
> record. It lives under `docs/archive/specs/` only because `docs/README.md:52-58`
> designates that directory as the single home for **all** dated design records; the
> `status: … NOT yet built` line above marks it live, and implementation tracks it via
> the PRs in §9.

This spec turns two Fleet-view gaps into buildable work:

1. **A per-box login link** — the Fleet overview shows deployments but gives no way
   to open the box and sign in.
2. **A Decommission action** — a delete control, gated by a copy-the-phrase
   confirmation, that removes a box from the fleet **and, for the first time, can
   actually destroy its Hetzner infrastructure.**

Feature 2 is plumbing. Feature 1 deliberately reverses the standing
"the broker exposes no destructive operation" posture: it is the *future deletion
executor* the architecture reserved (`hetzner-fleet-architecture.md:33`,
`AGENTS.md:96`). It ships only behind the guardrails in §3, the compensating
controls in §4, and the explicit, defaulted-safe policy deviation in §5. Every
current-state claim below is grounded in code as of 2026-07-22.

---

## 1. Current state (verified against code)

**Feature 2 — the link is missing but the data exists.**
- The overview response `DeploymentOverview` (`app/routers/fleet.py:550`) carries no
  host / IP / URL, so the console row `DeploymentRow`
  (`onebrain-web/src/components/fleet-panel.tsx:67`) cannot link out.
- A public address is recorded server-side as `external_run_url` at dispatch
  (`fqdn or public_ipv4`, `app/provisioning/hetzner/provisioner.py:410`), **but it is
  not a reliable box URL**: the box's success callback overwrites it with the raw public
  IPv4 (`deploy/box/onebrain_gate_report.py:327` → `apply_callback`), and it is a
  generic run field that can hold an arbitrary provider/workflow URL. The box's *stable,
  cert-matching* hostname is instead derivable from the deployment id + fleet DNS
  settings — see §6.

**Feature 1 — no delete path exists anywhere; this is greenfield.**
- `ControlPlaneStore` has `create/get/list` only (`app/controlplane/base.py:261`);
  there is **no `delete_deployment`**.
- The broker exposes no destroy: `destroy_box` raises `NotImplementedError` in both
  implementations (`app/provisioning/hetzner/broker.py:199`,
  `app/provisioning/hetzner/remote.py:302`); the client seam omits delete primitives
  *by design* (`app/provisioning/hetzner/client.py:9-12,151-153`); and the broker
  service serves only `/health` + `/v1/provision`
  (`app/provisioning/hetzner/broker_service.py:171-199`).
- The one sanctioned teardown path is **record-only**: `CustomerTeardownRequest`
  (`app/controlplane/base.py:224`) with create/approve endpoints
  (`app/routers/operator.py:1215,1291`). Its terminal result is the constant
  `execution_disabled` (`app/controlplane/base.py:54`); it never calls the broker.
  `validate_teardown_request` (`app/controlplane/base.py:457-467`) requires **two
  distinct approvers** and bars the requester from approving — i.e. three distinct
  identities to reach a terminal approved state.
- The teardown record **deliberately holds no infrastructure identifiers**
  (`app/controlplane/base.py:228`). The ids needed for a real teardown live
  separately, in the provisioning run's `erasure_manifest`
  (`server_id, volume_ids, dns_record_id, firewall_id`), written at
  `app/provisioning/hetzner/provisioner.py:380-390`.

---

## 2. What we build

- **F2:** add `login_url` to the overview and render the box name as a link.
- **F1:** add a teardown executor that either
  **(a)** destroys the Hetzner box through the broker when an erasure manifest is on
  file, or **(b)** record-only tombstones a box whose infra is already gone — both
  remove it from the fleet view. Authorization rides the **existing**
  `CustomerTeardownRequest`, with dual-control relaxed for a sole operator through an
  explicit, default-strict setting (§5).

---

## 3. Guardrails preserved (do not weaken)

- **P1-D — no single un-protect+delete primitive.** The new delete primitives on the
  client seam contain **no** un-protect / `change_protection` step and are never
  chained into one combined call. If a resource is ever delete-protected the delete
  fails; un-protecting is a separate, out-of-band, independently-authorized step. The
  Hetzner token stays broker-only.
- **Broker scope guard — every resource, not just the server.** MC hands the broker ids
  from a manifest, so the broker must independently prove **each** id belongs to this
  deployment before deleting it — not only the `server_id`. Before any delete the broker
  re-lists by `deployment_id` and confirms: the `server_id` is present and carries
  `managed-by=onebrain-fleet` (mirrors the provision-side allowlist,
  `app/provisioning/hetzner/broker_service.py:106`); each `volume_id` is attached to /
  labelled for that deployment; and the `firewall_id` and DNS record are the
  deployment's own (label / zone + name match). Any id that fails its check is refused,
  not deleted — a bad or stale manifest must never delete a *foreign* volume, firewall,
  or DNS record. The broker never deletes an id it was simply handed; that unchecked path
  is the fleet-wide kill switch P1-D exists to prevent.
- **Legal hold + evidence.** Teardown stays blocked under an active legal hold
  (`app/routers/operator.py:1228`) and still requires `legal_hold_evidence_ref` +
  `backup_retention_evidence_ref` on the record.
- **Approval nonce + TTL, and full audit** on every step (`_record_teardown_audit`).
- **Tombstone, not hard-delete.** The MC record is retired (`removed_at`), preserving
  the audit trail and erasure manifest; the *infrastructure* is what gets destroyed.
- **Record stays infra-identifier-free.** The executor reads ids from the
  provisioning-run manifest, never from the teardown record — the
  `app/controlplane/base.py:228` contract is untouched. (This separation is itself a
  P1-D property: authorization and target-ids come from two different records.)

---

## 4. Compensating controls

Because §5 removes distinct-human dual control, these are the mitigation the design
leans on and the reviewer should weigh:

1. Broker-only token custody + mTLS + source-restricted firewall (unchanged).
2. Broker-side scope guard: only a confirmed fleet-labelled server matching the
   deployment can be deleted.
3. Required legal-hold + backup/retention evidence refs; active-hold hard block.
4. One-time approval nonce with a TTL.
5. A typed copy-the-phrase confirmation at execute time (`decommission <deployment_id>`),
   re-checked server-side.
6. Tombstone (reversible at the record level) rather than hard-delete; full audit.
7. Digest-pinned broker image + explicit redeploy to enable `/v1/destroy` (§9).

---

## 5. Explicit deviation — relaxed dual-control (stated loudly)

The sanctioned record requires two distinct approvers plus a non-approving requester
(`app/controlplane/base.py:457-467`). OneBrain is a **sole-operator** organization
today, so that ceremony can never complete; left as-is, execution-on-top would be dead
on arrival and the operator's only real option is the manual Hetzner console — which
has **no** audit, evidence, or legal-hold gate at all.

**Change.** Two new settings, defaulting to the current strict behavior:

- `teardown_min_approvals: int = 2` — **bounded `1 ≤ n ≤ 2`, validated at settings
  load; fail closed.** An out-of-range value (e.g. an env typo
  `ONEBRAIN_TEARDOWN_MIN_APPROVALS=0`) must raise on load, never silently make a request
  executable with zero approvals. There is no "no approval" mode.
- `teardown_allow_self_approval: bool = false`

`validate_teardown_request` / `apply_teardown_approval` consult these instead of the
hard-coded `2` / distinct / requester-barred rules, and the execute endpoint gates on
`len(approver_ids) >= teardown_min_approvals` with that same bounded value. **All other
validation is unchanged** (evidence refs, nonce hash, TTL, legal hold).

**Residual risk, stated plainly.** With `min_approvals=1` and self-approval enabled, a
**single identity can authorize destruction of a live customer box.** This is accepted
for the sole-operator deployment because distinct-human dual control is not achievable,
and an unreachable ceremony would only drive the operator to the unaudited manual path.
The §4 controls are the mitigation. **Production / multi-operator deployments keep the
strict default** (`min_approvals=2`, self-approval off) and gain nothing from this
change.

**Precedent.** This mirrors `hetzner_allow_inprocess_broker`
(`app/provisioning/hetzner/broker.py:216-239`): a defaulted-safe, greppable,
explicitly opted-in escape hatch rather than deleted checks.

---

## 6. Feature 2 — login link (shipped: PR #48)

- Add `login_url: str = ""` to `DeploymentOverview` (`app/routers/fleet.py`).
- **Derive** `login_url`; do **not** read `external_run_url`. As established in review,
  the box's success callback overwrites a DNS box's `external_run_url` with its raw
  public IPv4 (`deploy/box/onebrain_gate_report.py:327` → `apply_callback`), and it is a
  generic run field that may carry an arbitrary provider/workflow URL. `fleet_overview`
  instead derives the address the same way the provisioner does, so it always matches the
  hostname Caddy holds a certificate for:
  `https://<_provider_hostname_label(id)>.<base_domain>` when the fleet is DNS-enabled
  (`fleet_dns_provider==hetzner` **and** `fleet_base_domain` **and** `fleet_dns_zone_id`),
  else `""`.
- **IP-only boxes get no link.** They serve plain HTTP on `:80`, and boxes render
  `ONEBRAIN_COOKIE_SECURE=true`, so a session cookie can't survive an `http://` origin —
  a link there would open the box but never keep the operator signed in. (This corrects
  an earlier draft that sourced the link from `external_run_url` and emitted `http://<ip>`
  for such boxes.)
- Add `login_url` to `FleetDeploymentOverview` (`onebrain-web/src/lib/onebrain-types.ts`);
  render the box name as `<a target="_blank" rel="noreferrer">` in `DeploymentRow` when
  present, plain text otherwise.
- Regenerate **both** OpenAPI contracts.

---

## 7. Feature 1 — teardown executor

### 7a. Phase A — broker teardown capability (P1-D-safe)

- **Client seam** (`app/provisioning/hetzner/client.py:132`): add `delete_server`,
  `delete_volume`, `delete_firewall`, `delete_dns_record`. **No** un-protect primitive
  (§3).
- Implement in the real transport (`urllib_client.py`) and the fake (`fake.py`) so
  every provisioner test exercises teardown offline.
- **Implement `InProcessHetznerBroker.destroy_box`** (`app/provisioning/hetzner/broker.py:199`):
  delete order **server → volumes → firewall → DNS**. Deleting the server first detaches
  its volumes, but Hetzner refuses to delete an *attached* volume, so the broker must
  **wait for each volume to report detached (or explicitly detach it) before
  `delete_volume`** — otherwise a live box with `hetzner_volume_size_gb > 0` loses its
  server yet keeps a billed, data-bearing volume behind. Extend the signature to carry
  `firewall_id` (the manifest has it, the current signature omits it); keep the
  `confirm=True` guard; apply the per-resource §3 scope guard before deleting **each**
  resource.
- **`POST /v1/destroy`** on the broker service
  (`app/provisioning/hetzner/broker_service.py:175` is the `/v1/provision` template),
  with its own `validate_destroy_request` and the existing `_authorize` bearer+mTLS
  check. Add matching `encode/decode_destroy_request` and
  `RemoteHetznerBroker.destroy_box` (`app/provisioning/hetzner/remote.py:302`),
  mirroring the `provision_box` transport.

### 7b. Phase B — MC control plane

- **Settings** (`app/config.py`): the two dual-control settings from §5.
- **State machine** (`app/controlplane/base.py`): add `TEARDOWN_REQUEST_APPROVED`
  (threshold met → executable) and `TEARDOWN_REQUEST_EXECUTED` /
  `_EXECUTION_FAILED`. Reaching the (relaxed) approval threshold now transitions to
  **APPROVED** instead of the old `execution_disabled` terminal
  (`app/controlplane/base.py:463-467`).
- **Execute endpoint** — `POST /api/operator/deployments/{id}/teardown-requests/{req_id}/execute`,
  alongside create/approve in `app/routers/operator.py` (shares `_require_admin` + the
  teardown store), **additionally gated `operator_mode`-only** because it reaches the
  broker (mirrors the fleet router's mount gate, `app/main.py:116`). It:
  1. requires the request in `APPROVED`; re-checks legal hold (TOCTOU);
  2. requires the typed phrase `decommission <deployment_id>` in the body, re-checked
     server-side (mirrors `confirmDelete`, `onebrain-web/src/components/users-panel.tsx:320`);
  3. resolves the **complete** erasure manifest for the deployment. A single
     deployment-keyed lookup of the *latest* run is **not** enough: an idempotent-reuse
     run (`broker.py:139-148`) returns empty `volume_ids` / `dns_record_id` /
     `firewall_id`, and the provisioner writes those empties into the later run's
     manifest — so using the latest run would delete only the server and **leak the data
     volume, DNS record, and firewall**. Resolve the original *creating* run's manifest
     (or accumulate the non-empty resource ids across the deployment's runs) so every
     resource is covered;
  4. **manifest with resources →** `broker.destroy_box(confirm=True)`;
     **no resolvable resources →** treat as **record-only**: tombstone plus a returned
     warning that no infra was touched. Record-only must **not** be inferred from
     manifest-absence alone — before tombstoning "no infra touched", the broker verifies
     no `managed-by=onebrain-fleet` server remains for the deployment; a manifest that is
     present but whose resources the provider reports **not found** takes this same
     warning path (so an operator can still clear a manually-deleted box's row) rather
     than failing;
  5. **tombstones** the deployment (new `removed_at` on `CustomerDeployment` + both
     stores; filtered out of `fleet_overview` and `list_deployments`), **revokes the
     box's fleet keys** so a resurrected box cannot heartbeat, writes audit, and
     transitions the request → `EXECUTED`.
- **Migration:** `removed_at` column + a `REQUIRED_ALEMBIC_REVISION` bump in
  `app/db/schema.py`. The teardown-status change is **not** free: migration
  `0028_customer_teardown_protocol` constrains `status` to `pending` /
  `execution_disabled` / `expired` via a CHECK (plus a terminal-result check tied to the
  old disabled result). The migration must **drop and recreate those constraints** to
  admit `APPROVED` / `EXECUTED` / `EXECUTION_FAILED`, or Postgres rejects the new statuses
  at write time.

### 7c. Phase C — console UI

A per-row **Decommission** flow in `fleet-panel.tsx`: collect the evidence refs
(legal-hold, backup/retention) → create request → approve (nonce) → **type the phrase
to confirm** → execute; surface the record-only warning. Lift the type-to-confirm
mechanic from `users-panel.tsx:458` (disabled button until the pasted value matches).
Client methods in `onebrain-web/src/lib/onebrain-client.ts`. Regenerate **both** OpenAPI
contracts.

---

## 8. Data-model, config & contract changes

| Area | Change |
|---|---|
| `CustomerDeployment` + both stores | `removed_at` (tombstone); filter in `list_deployments` / `fleet_overview` |
| `CustomerTeardownRequest` statuses | add `APPROVED`, `EXECUTED`, `EXECUTION_FAILED`; approval threshold → `APPROVED`; **migrate the 0028 status + terminal-result CHECK constraints** |
| `HetznerClient` seam | 4 delete primitives (no un-protect) |
| `InProcessHetznerBroker.destroy_box` | real implementation + scope guard + `firewall_id` |
| Broker service | `POST /v1/destroy` + `validate_destroy_request` |
| `RemoteHetznerBroker` | `destroy_box` transport + encode/decode |
| `app/config.py` | `teardown_min_approvals` (bounded 1–2, fail-closed), `teardown_allow_self_approval` |
| `DeploymentOverview` / `FleetDeploymentOverview` | `login_url` (derived from deployment id + fleet DNS settings) |
| OpenAPI | regenerate `openapi.json` **and** `openapi.customer.json` |

---

## 9. Rollout sequence

1. **PR1 — box link (F2).** Small, independent, risk-free.
2. **PR2 — Phase A broker teardown.** The invariant-touching change, isolated for
   focused review. **Operational dependency:** the broker runs a digest-pinned image
   of this code (`deploy/broker/docker-compose.yml:9`); the broker host must be
   redeployed with a **new image digest** before MC can call `/v1/destroy`. Until then
   the MC execute endpoint's infra path fails closed and only the record-only path
   works.
3. **PR3 — Phase B + C.** MC execute endpoint, dual-control settings, tombstone,
   migration, and the console flow.

---

## 10. Docs & invariants to update **when the code lands** (not now)

- `AGENTS.md` architecture invariants — the "exposes no destructive operation" and
  "Do not reintroduce an in-process broker or loosen the production guard" lines need
  revision to describe the guarded `/v1/destroy`.
- `hetzner-fleet-architecture.md` §"Destructive lifecycle" — rewrite (this spec
  supersedes it).
- `deploy/broker/README.md:60` — "exposes only `/health` and `/v1/provision`;
  teardown is not implemented" — update.
- Docstrings that assert destroy is deliberately absent / Phase-4-OUT
  (`client.py:9-12,151-153`, `broker.py:8-10,69-71`).
- `.env.example` — document the two dual-control settings and any broker destroy
  config.

---

## 11. Testing & acceptance criteria

- **Broker:** destroy happy path (server→volume→firewall→DNS, volumes detached before
  delete); **rejection of a forged / foreign id for *every* resource** (a volume /
  firewall / DNS id not belonging to the deployment is refused, not just a bad server
  id); `confirm=False` guard; no token/credential in logs
  (`tests/test_hetzner_remote_broker.py`, `tests/test_hetzner_broker_assets.py`).
- **Manifest resolution:** a deployment whose latest run is an idempotent reuse (empty
  resource ids) still tears down the volume / DNS / firewall from the original creating
  run — no leak; a `teardown_min_approvals` outside `1..2` fails settings load.
- **Dual-control policy:** strict default still needs two distinct approvers;
  `min_approvals=1 + self_approval` lets one identity reach `APPROVED`; evidence refs
  and legal hold still enforced under both.
- **Execute endpoint** (`tests/test_fleet.py`): auth (operator_mode-only), legal-hold
  block, phrase mismatch → refused, manifest-present → broker called, manifest-absent →
  record-only + warning, keys revoked, request → `EXECUTED`, audit written.
- **Tombstone:** a retired deployment disappears from `fleet_overview` /
  `list_deployments` and its audit + manifest survive.
- **F2:** overview **derives** `https://<label>.<base_domain>` for DNS fleets and `""`
  for IP-only fleets (no plain-HTTP secure-cookie link); it never reads `external_run_url`.
- Full gate: `pytest -q --basetemp=C:/obt`, `verify_requirements_lock.py`, frontend
  `lint && typecheck && test && build`, both OpenAPI contracts regenerated.

---

## 12. Non-goals & open risks

- **Not GDPR data-erasure.** Infra teardown destroys the server and its data volume,
  but Hetzner root-disk Backups (`erasure_manifest.hetzner_backups`), the offsite
  encrypted `pg_dump` prefix, and the box's sealed secret bundle
  (`box_secret_bundles`) are **not** removed by `destroy_box`; they follow the
  separate retention / deletion-tombstone contract
  (`2026-07-11-deletion-tombstone-contract-design.md`). "Decommissioned" must not be
  read as "erased." Purging the sealed secret bundle on teardown is a recommended
  Phase-B follow-up (hygiene, not correctness).
- No batch / multi-box teardown; one deployment per action.
- No Hetzner delete-protection un-protect (kept out per P1-D).
- The relaxed dual-control (§5) is the principal risk and is accepted deliberately for
  the sole-operator deployment; revisit if OneBrain gains additional operators (prefer
  reverting to `min_approvals=2`).

# OneBrain fleet decommission & teardown executor

```
status:     draft v1 (implementable spec) — NOT yet built
date:       2026-07-22
owner:      OneBrain / Mission Control
supersedes: hetzner-fleet-architecture.md §"Destructive lifecycle" (on landing)
scope:      Mission Control fleet view (mc.onlyonebrain.com) — operator_mode only
```

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
- The box's public login address already exists server-side as
  `external_run_url` (`fqdn or public_ipv4`), set at
  `app/provisioning/hetzner/provisioner.py:410` and persisted on the provisioning
  run (`app/provisioning/runs.py:67`). It is authoritative for both DNS boxes
  (`https://<id>.onlyonebrain.com`) and IP-only / dev-gate boxes.

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
- **Broker scope guard.** Before any delete the broker re-lists servers by
  `deployment_id` and confirms the target `server_id` is present **and** carries
  `managed-by=onebrain-fleet` (mirrors the provision-side allowlist,
  `app/provisioning/hetzner/broker_service.py:106`). The broker never deletes an id it
  was simply handed — that would be the fleet-wide kill switch P1-D exists to prevent.
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

- `teardown_min_approvals: int = 2`
- `teardown_allow_self_approval: bool = false`

`validate_teardown_request` / `apply_teardown_approval` consult these instead of the
hard-coded `2` / distinct / requester-barred rules. **All other validation is
unchanged** (evidence refs, nonce hash, TTL, legal hold).

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

## 6. Feature 2 — login link

- Add `login_url: str = ""` to `DeploymentOverview` (`app/routers/fleet.py:550`).
- In `fleet_overview` (`app/routers/fleet.py:590`) join the provisioning-run store
  (`get_provisioning_run_store()`), take each deployment's latest run, and set
  `login_url` from `external_run_url` — `https://` for an FQDN, `http://` for an
  IP-only box, `""` when there is no run. One extra store read; negligible at fleet
  size ~5. Do **not** re-derive from `fleet_base_domain` — that silently breaks the
  DNS-disabled / dev-gate boxes.
- Add `login_url` to `FleetDeploymentOverview`
  (`onebrain-web/src/lib/onebrain-types.ts:1002`); render the box name as
  `<a target="_blank" rel="noopener">` in `DeploymentRow` when present, plain text
  otherwise.
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
  delete order **server → volumes → firewall → DNS**; extend the signature to carry
  `firewall_id` (the manifest has it, the current signature omits it); keep the
  `confirm=True` guard; apply the §3 scope guard before deleting.
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
  3. reads the erasure manifest from the provisioning-run store, keyed by deployment;
  4. **manifest present →** `broker.destroy_box(confirm=True)`;
     **absent → record-only** tombstone plus a returned warning that no infra was
     touched (this is the path that clears already-dead deployments and
     hand-registered dev gates);
  5. **tombstones** the deployment (new `removed_at` on `CustomerDeployment` + both
     stores; filtered out of `fleet_overview` and `list_deployments`), **revokes the
     box's fleet keys** so a resurrected box cannot heartbeat, writes audit, and
     transitions the request → `EXECUTED`.
- **Migration:** `removed_at` column + a `REQUIRED_ALEMBIC_REVISION` bump in
  `app/db/schema.py`. The new statuses are string values on the existing teardown
  table (migration 0028); no column change there.

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
| `CustomerTeardownRequest` statuses | add `APPROVED`, `EXECUTED`, `EXECUTION_FAILED`; approval threshold → `APPROVED` |
| `HetznerClient` seam | 4 delete primitives (no un-protect) |
| `InProcessHetznerBroker.destroy_box` | real implementation + scope guard + `firewall_id` |
| Broker service | `POST /v1/destroy` + `validate_destroy_request` |
| `RemoteHetznerBroker` | `destroy_box` transport + encode/decode |
| `app/config.py` | `teardown_min_approvals`, `teardown_allow_self_approval` |
| `DeploymentOverview` / `FleetDeploymentOverview` | `login_url` |
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

- **Broker:** destroy happy path (server→volume→firewall→DNS order); **rejection of a
  forged / foreign server id** (not fleet-labelled, or deployment mismatch);
  `confirm=False` guard; no token/credential in logs
  (`tests/test_hetzner_remote_broker.py`, `tests/test_hetzner_broker_assets.py`).
- **Dual-control policy:** strict default still needs two distinct approvers;
  `min_approvals=1 + self_approval` lets one identity reach `APPROVED`; evidence refs
  and legal hold still enforced under both.
- **Execute endpoint** (`tests/test_fleet.py`): auth (operator_mode-only), legal-hold
  block, phrase mismatch → refused, manifest-present → broker called, manifest-absent →
  record-only + warning, keys revoked, request → `EXECUTED`, audit written.
- **Tombstone:** a retired deployment disappears from `fleet_overview` /
  `list_deployments` and its audit + manifest survive.
- **F2:** overview returns `login_url` for DNS and IP-only boxes; row links only when
  present.
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

# OneBrain Development Release Promotion Gate: Implementation Plan

**Date:** 2026-07-13
**Status:** Ready for implementation after review
**Design:** `docs/superpowers/specs/2026-07-13-onebrain-dev-release-promotion-gate-design.md`

## Objective

Implement the approved release-promotion gate without duplicating the existing
Hetzner provisioner, rollout reducer, desired-state path, or update planner.

The finished path is:

```text
green main CI
  -> prepare and sign immutable dev candidate
  -> register candidate in Mission Control
  -> automatic single-target dev rollout
  -> authenticated success plus matching healthy heartbeat
  -> offline production signature
  -> explicit operator approval
  -> one-at-a-time updates to explicitly selected customers
```

Application-code implementation and operational activation are separate. The code
can be shipped after automated verification. Creating the billed Hetzner dev
server, installing production secrets, and enabling the hard gate happen later
through the activation runbook with explicit operator confirmation.

## Implementation Principles

- Add tests before each behavior change.
- Keep release manifests immutable; store promotion lifecycle separately.
- Keep `compute_update_plan` as the shared static safety decision.
- Reuse named-target fleet rollouts for the dev server.
- Keep the production release private key offline.
- Use Postgres compare-and-set updates and matching memory-store locks.
- Fail closed when promotion enforcement is enabled.
- Do not infer historical dates without authenticated evidence.
- Do not expose manual success, health, or backup shortcuts in the web console.
- Commit each task independently so risky changes can be reviewed or reverted in
  isolation.

## Dependency Order

```text
schema/domain
  -> persistence
  -> promotion state machine and candidate trust
  -> candidate API and automatic dev dispatch
  -> planner and telemetry gates
  -> rollout completion/failure integration
  -> restart-safe sequential fleet behavior
  -> dev provisioning/designation
  -> operator promotion API
  -> read models and dates
  -> Mission Control UI
  -> CI registration workflow
  -> runbook and full verification
```

## Task 1: Add the schema and domain records

**Files**

- Create `migrations/versions/0022_release_promotion_gate.py`.
- Modify `app/controlplane/base.py`.
- Modify `app/controlplane/orchestration.py`.
- Modify `tests/test_postgres_schema_validation.py`.
- Modify `tests/test_controlplane.py`.

**Test first**

Add schema assertions for:

- `control_deployments.is_release_gate`.
- `control_deployments.current_version_deployed_at`.
- Denormalized `last_heartbeat_at` and `last_heartbeat_healthy` columns used by the
  planner without reaching into the fleet store.
- The partial unique index allowing at most one active release gate.
- `control_release_promotions` with the approved state and timestamp columns.
  Its `gate_deployment_id` is nullable while a candidate is waiting for a healthy
  gate and is bound when dev dispatch starts.
- Append-only `control_release_promotion_events` plus mutation-blocking triggers.
- Persisted `ring_batch_size`, `only_deployment_ids`, and
  `include_manual_pinned` columns on `control_fleet_rollouts`.
- A downgrade that removes only this migration's objects.

Add domain tests for release-gate invariants, promotion-state validation, and the
new fleet rollout fields.

**Implement**

Add immutable dataclasses:

- `ReleasePromotion`.
- `ReleasePromotionEvent`.
- The deployment timestamps and gate marker on `CustomerDeployment`.
- The persisted target/batch fields on `FleetRolloutRun`.

Add constants for promotion states and actions. Keep transition logic out of the
dataclasses; it belongs in the promotion service in Task 3.

The migration backfills `current_version_deployed_at` from the newest successful
rollout with a matching target version, then from a successful matching
provisioning run. It leaves unverifiable rows `NULL`.

**Verify**

```powershell
py -m pytest -q tests/test_postgres_schema_validation.py tests/test_controlplane.py
```

**Commit**

`Add release promotion schema and domain records`

## Task 2: Implement memory and Postgres persistence

**Files**

- Modify `app/controlplane/base.py`.
- Modify `app/controlplane/memory.py`.
- Modify `app/controlplane/postgres.py`.
- Modify `tests/test_controlplane.py`.
- Modify `tests/test_postgres_schema_validation.py`.

**Test first**

Cover both stores for:

- Idempotent creation of an identical candidate promotion.
- Conflict when a version is reused with different immutable manifest content.
- Get/list promotion and event history.
- Expected-state compare-and-set transitions.
- Atomic release-gate designation and replacement.
- Rejection of two active gates.
- Atomic production signature, release activation, approval transition, and event.
- Customer pause and resume-with-note.
- Release yank.
- Deployment telemetry updates.
- `current_version_deployed_at` mutation on authenticated apply.
- Persistence of fleet target and batch settings across a store reload.
- Backward-compatible memory persistence when new fields are absent.

**Implement**

Extend `ControlPlaneStore` with focused methods rather than one generic update
dictionary:

- Candidate promotion create/get/list.
- Expected-state promotion transition.
- Atomic signature-and-approval operation.
- Append/list immutable promotion events.
- Get/designate the development gate.
- Update denormalized deployment heartbeat state.
- Apply a successful deployment version with installation timestamp.

Postgres operations that change state and write an event use one connection, one
cursor, and one commit. Memory operations use the store's existing lock. Do not
coordinate the promotion transaction through `PlatformStore`.

Extend fleet rollout serialization and Postgres row mapping with persisted batch
and target fields.

**Verify**

```powershell
py -m pytest -q tests/test_controlplane.py tests/test_postgres_schema_validation.py
```

**Commit**

`Persist release promotion and gate state`

## Task 3: Build the pure promotion and candidate service

**Files**

- Create `app/controlplane/promotion.py`.
- Modify `app/trust/release.py`.
- Modify `app/config.py`.
- Modify `.env.example`.
- Create `tests/test_release_promotion.py`.
- Modify `tests/test_trust.py`.

**Test first**

Add table-driven tests for every allowed and refused state transition. Add tests
for:

- Canonical manifest digest stability.
- Candidate version format `YYYY.MM.DD.<run_number>`.
- Development signature verification with the dev public key.
- Rejection of production-key/dev-key confusion.
- Preparing a full manifest by overlaying changed module digests on the last
  approved manifest.
- Bootstrap preparation from an explicitly supplied trusted baseline.
- Exact immutable-field comparison during production signature attachment.
- Sanitization of stored failure reasons.

**Implement**

Create a small service with pure helpers for:

- `prepare_candidate(...)`: produce the complete canonical manifest. The current
  OneBrain workflow replaces all three OneBrain module images. Unchanged modules
  from other repositories are copied from the latest approved manifest when they
  exist.
- `register_candidate(...)`: validate version, manifest digest, module/image
  coverage, migration classification, and development signature.
- `decide_transition(...)`: return the next state or a stable error.
- `verify_production_signature_match(...)`: verify the offline signature over the
  exact stored fields.

Add configuration:

- `ONEBRAIN_RELEASE_PROMOTION_REQUIRED`.
- `ONEBRAIN_DEV_RELEASE_VERIFY_PUBLIC_KEY`.
- `ONEBRAIN_RELEASE_CANDIDATE_KEY_ID`.
- `ONEBRAIN_RELEASE_CANDIDATE_KEY_HASH`.

Reuse the existing Ed25519 canonicalization and callback-secret hash/verify
primitives. Do not add a second cryptography format.

**Verify**

```powershell
py -m pytest -q tests/test_release_promotion.py tests/test_trust.py
```

**Commit**

`Add release promotion state machine and trust checks`

## Task 4: Add candidate authentication, API, and automatic dev dispatch

**Files**

- Modify `app/routers/operator.py`.
- Modify `app/controlplane/fleet_runner.py` only through existing public helpers.
- Modify `app/controlplane/desired_state.py` if draft dev candidates are currently
  filtered before the gate-specific trust decision.
- Modify `tests/test_controlplane.py`.
- Modify `tests/test_fleet_orchestration.py`.
- Modify `tests/test_desired_state.py`.

**Test first**

Cover:

- Candidate endpoint missing, malformed, wrong-ID, and wrong-secret credentials.
- Candidate credentials cannot call operator-only promotion actions.
- `action=prepare` returns a canonical complete manifest without persisting it.
- `action=register` requires a valid dev signature and persists `dev_pending`.
- Identical registration is a successful no-op.
- Conflicting registration returns HTTP 409.
- A healthy designated gate causes one named-target dev rollout and
  `dev_deploying`.
- Missing/unhealthy gate keeps the candidate `dev_pending`.
- Synchronous dispatch failure produces `dev_failed` and an event.
- No customer ID appears in the automatic dev rollout target set.

**Implement**

Add `POST /api/operator/release-candidates` with two machine-authenticated request
shapes:

- `prepare`: merge changed module artifacts onto the latest approved baseline and
  return the canonical manifest to sign.
- `register`: verify and persist the signed candidate, then dispatch only to the
  release gate.

Registration stores the immutable release manifest as `draft`; only operator
approval activates it for customers. When desired state is built for the designated
gate, it embeds `promotion.dev_signature`. Customer desired state always embeds
the manifest's production `signature`. Tests must prove these signatures cannot
cross surfaces.

Use `Authorization: Bearer` plus `X-OneBrain-Release-Key-Id`. This route is mounted
only on the operator surface. It does not accept a normal customer service key or
fleet key.

Automatic dispatch calls the existing named-target fleet rollout path with the
gate deployment as the sole target, batch size `1`, failure tolerance `0`, and no
manual/pinned override.

**Verify**

```powershell
py -m pytest -q tests/test_controlplane.py tests/test_fleet_orchestration.py tests/test_desired_state.py
```

**Commit**

`Register candidates and dispatch the development gate`

## Task 5: Enforce promotion and heartbeat state in the shared planner

**Files**

- Modify `app/controlplane/base.py`.
- Modify `app/controlplane/memory.py`.
- Modify `app/controlplane/postgres.py`.
- Modify `app/routers/fleet.py`.
- Modify `tests/test_controlplane.py`.
- Modify `tests/test_fleet.py`.

**Test first**

Add a denial matrix for:

- Missing promotion.
- Unverified dev candidate on a customer.
- Approved release on the designated gate.
- Dev candidate on any non-gate deployment.
- Paused and yanked releases.
- Draft releases outside the current dev candidate.
- Missing or invalid production signature even when the legacy signature flag is
  disabled.
- Stale, missing, and unhealthy deployment telemetry.
- Existing module, pinned-policy, backup, and restore acknowledgement gates.
- Report-only mode returning diagnostics without blocking legacy behavior.

**Implement**

Heartbeat ingestion denormalizes only safety metadata into the deployment row:

- receipt time;
- healthy boolean;
- reported version and migration if needed for the read model.

`compute_update_plan` receives the deployment, release, promotion, and current
clock. It applies promotion checks before module/migration planning. The store
loads all static inputs so `start_rollout` and successful completion re-run the
same decision.

Add `warnings: list[str]` to `UpdatePlan` and `UpdatePlanOut`. Report-only mode
keeps the legacy `allowed` result while placing promotion denials in `warnings`;
enforced mode returns the same code as the blocking `reason`. This makes the
pre-enforcement audit visible without weakening the final gate.

Freshness is the greater of two configured heartbeat intervals or ten minutes.
When `ONEBRAIN_RELEASE_PROMOTION_REQUIRED=false`, new denial reasons are returned
as diagnostics but do not block legacy customer paths. When true, they fail
closed.

Successful authenticated apply updates deployment version, migration, module
versions, and `current_version_deployed_at` in the same transaction.

**Verify**

```powershell
py -m pytest -q tests/test_controlplane.py tests/test_fleet.py
```

**Commit**

`Enforce release promotion in update planning`

## Task 6: Verify dev heartbeats and freeze customer failures

**Files**

- Modify `app/controlplane/pull_reconcile.py`.
- Modify `app/controlplane/rollout_exec.py`.
- Modify `app/routers/rollouts.py` if callback completion bypasses the shared store
  hook.
- Modify `tests/test_pull_reconcile.py`.
- Modify `tests/test_rollout_exec.py`.
- Modify `tests/test_controlplane.py`.

**Test first**

Cover:

- Dev rollout success without a matching heartbeat remains unverified.
- Matching healthy heartbeat verifies the exact rollout attempt.
- Version, migration, enabled-module, health, attempt-ID, rollback, and timeout
  mismatches produce `dev_failed`.
- Duplicate matching heartbeats do not duplicate timestamps or events.
- A late success cannot revive a timed-out candidate.
- Customer failure, rollback, health failure, or timeout pauses an approved
  release.
- A paused release blocks another in-flight completion at the planner recheck.
- Yank wins races against success.
- Railway callback and Hetzner pull paths produce identical promotion behavior.

**Implement**

Keep terminal rollout handling centralized in the control-plane store. When a
rollout fails:

- gate rollout: transition `dev_deploying -> dev_failed`;
- customer rollout: transition `customer_approved -> customer_paused`.

After a gate rollout reports success, evaluate the heartbeat payload against the
stored candidate and rollout ID. Only then transition to `dev_verified`.

Do not copy customer logs or free-form remote errors into promotion state. Store a
stable reason code plus a bounded sanitized detail.

**Verify**

```powershell
py -m pytest -q tests/test_pull_reconcile.py tests/test_rollout_exec.py tests/test_controlplane.py
```

**Commit**

`Verify dev releases and pause failed customer delivery`

## Task 7: Make customer fleet rollout sequential and restart-safe

**Files**

- Modify `app/controlplane/orchestration.py`.
- Modify `app/controlplane/fleet_runner.py`.
- Modify `app/controlplane/memory.py`.
- Modify `app/controlplane/postgres.py`.
- Modify `app/routers/operator.py`.
- Modify `app/controlplane/reconcile_scheduler.py`.
- Modify `tests/test_fleet_orchestration.py`.
- Modify `tests/test_pull_reconcile.py`.

**Test first**

Add restart simulation proving that persisted fields preserve:

- batch size `1`;
- exact deployment target set;
- manual/pinned override choice;
- failure tolerance `0`.

Add tests proving:

- Promotion-required fleet requests reject an empty implicit-all target list.
- They reject batch size other than `1` and nonzero failure tolerance rather than
  silently weakening operator intent.
- The next customer is not dispatched until the current child succeeds.
- One failure pauses the release and prevents another child from opening.
- Scheduler and callback reconciliation read persisted policy instead of legacy
  defaults.

**Implement**

Thread persisted rollout policy through `plan_and_start_fleet_rollout`,
`reconcile_fleet_rollout`, `advance_fleet_on_child`, and the scheduler. Remove the
restart degradation to `ring_batch_size=0` for new rows. Continue reading legacy
rows with their current behavior while promotion enforcement is false.

When enforcement is true, require explicit deployment IDs. The UI can offer
**Select all**, but it must send the concrete IDs captured at confirmation time.

**Verify**

```powershell
py -m pytest -q tests/test_fleet_orchestration.py tests/test_pull_reconcile.py
```

**Commit**

`Persist sequential fleet rollout policy`

## Task 8: Add development server provisioning and designation

**Files**

- Modify `app/routers/provisioning.py`.
- Modify `app/provisioning/service.py` only if orchestration extraction belongs
  there.
- Modify `app/routers/operator.py`.
- Modify `app/provisioning/hetzner/provisioner.py` only for the separate dev release
  public key selection.
- Modify `app/provisioning/hetzner/render.py`.
- Modify `tests/test_provisioning.py`.
- Modify `tests/test_hetzner_provisioner.py`.
- Modify `tests/test_controlplane.py`.

**Test first**

Cover:

- Dev provisioning forces dedicated Hetzner, Nuremberg, development, internal,
  auto, synthetic-data mode, and the `onebrain_only` bundle currently published by
  this repository.
- The initial baseline release is already production-signed and trusted.
- Owner email and one-time credential behavior remains identical to a customer
  stack.
- The dev box receives the dev release verification public key; customer boxes
  continue receiving the production public key.
- Designation refuses inactive, unenrolled, stale, unhealthy, or non-dedicated
  deployments.
- Replacement swaps the marker atomically.

**Implement**

Extract the reusable provisioning orchestration from the route so the new
`POST /api/operator/development-gate/provision` endpoint does not call another
route function. The endpoint supplies fixed safety fields and accepts only owner
email, trusted initial version, and optional internal name.

Implement `GET /api/operator/development-gate` and transactional
`PUT /api/operator/development-gate/{deployment_id}`.

The first implementation deliberately provisions OneBrain's three modules because
this repository currently publishes only those three images. Communication and
assistant modules remain blocked by module-coverage checks until their build
pipelines participate in the same signed candidate.

**Verify**

```powershell
py -m pytest -q tests/test_provisioning.py tests/test_hetzner_provisioner.py tests/test_controlplane.py
```

**Commit**

`Provision and designate the development release gate`

## Task 9: Add operator promotion actions and read models

**Files**

- Modify `app/routers/operator.py`.
- Modify `app/routers/fleet.py`.
- Modify `tests/test_controlplane.py`.
- Modify `tests/test_fleet.py`.

**Test first**

Cover all approved routes:

- Retry dev.
- Upload production signature.
- Approve.
- Pause.
- Resume with mandatory note.
- Yank.
- Read gate, promotion, dates, and event history.

Test operator-only authorization, invalid-state conflicts, idempotent same-signature
upload, actor/timestamp/event recording, and customer-surface 404 behavior.

**Implement**

Extend `ReleaseOut` with promotion state rather than returning a separate
N+1-fetched endpoint per release. Extend deployment and fleet overview output with:

- `created_at`;
- `current_version_deployed_at`;
- `is_release_gate`;
- reported version, heartbeat time, and health already available from fleet data.

Remove mutating operator endpoints that let a production user record synthetic
health/backup success or force a rollout terminal success. Keep authenticated
machine callback routes intact.

**Verify**

```powershell
py -m pytest -q tests/test_controlplane.py tests/test_fleet.py
```

**Commit**

`Expose development promotion controls in Mission Control`

## Task 10: Build the Mission Control UI

**Files**

- Modify `onebrain-web/src/lib/onebrain-types.ts`.
- Modify `onebrain-web/src/lib/onebrain-client.ts`.
- Modify `onebrain-web/src/components/operator-panel.tsx`.
- Modify `onebrain-web/src/components/fleet-panel.tsx`.
- Modify the existing admin stylesheet used by these panels.
- Regenerate `onebrain-web/src/lib/openapi.json`.

**Implement in small UI slices**

1. Add typed promotion, event, gate, and date models plus client functions.
2. Add the Development gate card with provision, retry, and replacement states.
3. Expand the Releases view with dev progress, verified/signature/approval states,
   and valid actions only.
4. Filter customer release selectors to `customer_approved`.
5. Show server-created, version-installed, last-seen, and drift values.
6. Require explicit customer selection for fleet rollout and send the concrete ID
   set.
7. Remove **Backup ok**, **Health ok**, and force-success controls.
8. Preserve keyboard access, labels, busy-state disabling, and mobile layout.

Do not split state enforcement into the client. Buttons and filtering are a usable
projection of server decisions.

**Verify after each slice**

```powershell
npm run typecheck
npm run lint
npm run build
```

Run these commands from `onebrain-web`.

**Commit**

`Add development promotion workflow to Mission Control`

## Task 11: Register green CI releases automatically

**Files**

- Create `scripts/register_release_candidate.py`.
- Create `tests/test_register_release_candidate.py`.
- Modify `.github/workflows/tests.yml`.
- Modify `.github/workflows/publish-images.yml` only if another output is required.
- Modify the workflow-contract check in `.github/workflows/tests.yml` or extract it
  to a test file if the inline assertion becomes unwieldy.

**Test first**

The script tests cover:

- Candidate version generation from UTC date and GitHub run number.
- Prepare/register two-step exchange.
- Canonical manifest preservation between the two requests.
- Dev signing through the existing trust code.
- Auth headers.
- Idempotent rerun behavior.
- Conflict and unavailable-Mission-Control exit codes.
- No production private-key input or environment variable.

**Implement**

After `publish-images` succeeds on a push to `main`, add a
`register-release-candidate` job using GitHub Environment `release-dev`. Guard it
with repository variable `ONEBRAIN_RELEASE_CANDIDATE_ENABLED == 'true'` so merging
the code before infrastructure activation skips registration rather than failing
`main`. It receives the three image digests from the reusable publish workflow and
uses:

- `ONEBRAIN_MC_URL`.
- `ONEBRAIN_RELEASE_CANDIDATE_KEY_ID`.
- `ONEBRAIN_RELEASE_CANDIDATE_KEY`.
- `ONEBRAIN_DEV_RELEASE_PRIVATE_KEY`.

Write the dev private key to a permission-restricted temporary file, mask secrets,
delete the file in an `always()` cleanup step, and never print the manifest
signature or bearer secret. The production release key is not a workflow secret.

**Verify**

```powershell
py -m pytest -q tests/test_register_release_candidate.py
py -m pytest -q tests/test_controlplane.py tests/test_trust.py
```

Also run the repository's workflow contract and secret-pattern checks through the
full test suite.

**Commit**

`Register green builds as development candidates`

## Task 12: Document activation and operator recovery

**Files**

- Create `docs/development-release-gate-runbook.md`.
- Modify `docs/mission-control-standup.md`.
- Modify `docs/deployment.md`.
- Modify `.env.example` if Task 3 did not fully document each variable.

**Document**

- Generate the dev signing key and candidate bearer secret.
- Store the dev private key and CI bearer secret in GitHub Environment
  `release-dev`.
- Configure only public/hash material on Mission Control.
- Leave repository variable `ONEBRAIN_RELEASE_CANDIDATE_ENABLED=false` until the
  dev server is enrolled and the candidate endpoint is reachable.
- Keep the production private key offline.
- Deploy in report-only mode.
- Provision the dedicated synthetic OneBrain dev server.
- Verify enrollment, heartbeat, backup, and baseline version.
- Designate the gate.
- Run one harmless candidate through dev verification.
- Sign offline and approve it.
- Confirm an unapproved customer plan is blocked.
- Enable `ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true`.
- Pause, retry, resume-with-note, yank, and replace-gate procedures.
- Restore report-only mode only as an emergency rollback, with an audit note and no
  customer rollout in progress.

The runbook explicitly marks billed Hetzner provisioning and external secret
changes as operator-confirmed actions, not automated test steps.

**Verify**

```powershell
rg -n "TODO|TBD|placeholder" docs/development-release-gate-runbook.md docs/mission-control-standup.md docs/deployment.md
git diff --check
```

**Commit**

`Document development release gate operations`

## Task 13: Run full verification and ship the code

**Checks**

```powershell
py -m pytest -q
npm run typecheck
npm run lint
npm run build
git diff --check
git status --short
```

Run the npm commands from `onebrain-web`. Regenerate OpenAPI before the final
frontend checks and verify it has no unexpected unrelated drift.

Perform a final targeted review for:

- All rollout entry points calling the shared planner.
- No customer path accepting a dev signature.
- No CI path accepting or reading the production private key.
- Compare-and-set transitions and atomic event writes.
- One-at-a-time persisted customer targeting.
- No raw customer logs/content in failure metadata.
- No production UI force-success/health/backup controls.
- Migration upgrade and downgrade consistency.

If all checks pass and the worktree contains only task-related changes, follow the
repository shipping workflow: stage task files, commit any final verification
adjustment, push the feature branch, fast-forward local `main`, merge the feature
branch, and push `main`. Stop without shipping on failures, conflicts, secrets, or
unrelated changes.

**Commit if needed**

`Complete development release promotion gate`

## Task 14: Activate on Mission Control after code shipment

This is an operational task, not part of the code-shipping commit. It requires
Mission Control credentials, GitHub Environment secret changes, and explicit
confirmation before creating the billed Hetzner server.

Follow the runbook in this order:

1. Configure report-only settings and restart Mission Control.
2. Install dev public key and candidate key hash on Mission Control.
3. Install dev private key and candidate bearer secret in GitHub Environment
   `release-dev`.
4. Provision the dedicated synthetic dev server.
5. Confirm healthy baseline heartbeat and designate it as the gate.
6. Set repository variable `ONEBRAIN_RELEASE_CANDIDATE_ENABLED=true`.
7. Merge a harmless test change to create the first automatic candidate.
8. Confirm dev rollout, update report, and matching heartbeat produce
   `dev_verified`.
9. Sign the stored manifest offline and approve it.
10. Confirm the approved release becomes selectable while an unapproved release is
   rejected.
11. Enable `ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true` and restart Mission Control.
12. Run a final dry plan only; do not update a live customer during activation.

Activation is complete when the Development gate card is healthy, dates are
truthful, automatic dev delivery works, and every customer path rejects an
unapproved release.

## Final Acceptance Matrix

| Scenario | Expected result |
|---|---|
| Green main CI with healthy gate | Candidate automatically reaches only dev |
| No gate or stale gate | Candidate recorded; dispatch and approval blocked |
| Dev reports success but heartbeat mismatches | `dev_failed`; customers blocked |
| Dev verifies but production signature missing | Approval blocked |
| Valid offline signature plus operator approval | Release selectable for chosen customers |
| Empty implicit-all fleet request | Rejected when enforcement is enabled |
| First customer fails | Release pauses; no second customer dispatch |
| Mission Control restarts mid-rollout | Exact target set and batch size `1` persist |
| Release yanked during rollout | Completion cannot apply it |
| Historical install date lacks evidence | UI shows **Unknown** |
| CI credential calls approval endpoint | Unauthorized |
| Customer or fleet key reads promotion controls | Unauthorized or surface 404 |

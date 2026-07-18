# Full-Stack Development Gate Promotion Repair: Implementation Plan

**Goal:** Safely expand the enrolled legacy development gate from three Core services
to the exact eight-service OneBrain composition, while preserving restore-required
acknowledgement, immutable release manifests, promotion linkage, and heartbeat proof.

**Working tree:** `.codex-worktrees/dev-gate-adoption`

**Branch:** `codex/dev-gate-adoption` / PR #19

## Invariants

- Candidate `2026.07.18.270` remains immutable and failed.
- Automatic candidate registration never acknowledges `restore_required`.
- Only an authenticated administrator can retry with an explicit acknowledgement.
- A manual, unlinked rollout cannot advance a release promotion.
- The only accepted pre-expansion active sets are exact Core or exact full-stack.
- The target development manifest must contain exactly all eight services.
- Missing services are never marked active before an authenticated successful report.
- Module activation, deployment version update, and rollout completion are atomic.
- Customer deployments retain the provisioning-ledger requirement and production
  signature/approval path.
- The production private key stays offline.

## Task 1: Centralize the development-gate module policy

**Files:**

- Add `app/controlplane/development_gate.py`.
- Modify `app/controlplane/rollout_exec.py`.
- Modify `app/routers/operator.py`.
- Add or modify `tests/test_development_gate.py` and `tests/test_rollout_exec.py`.

**Steps:**

1. Define immutable Core and full-stack module ID sets in one dependency-light
   control-plane module.
2. Add a pure validator that accepts an exact Core or exact full-stack current set
   and requires an exact full-stack target set.
3. Remove duplicate module constants from router/rollout files.
4. Keep target eligibility focused on gate identity, key-bound fresh heartbeat,
   encrypted bundle, and applied secrets epoch; do not claim missing modules are
   already installed.
5. Reject partial, extra, or foreign current/target sets with stable reason codes.
6. Verify customer deployments cannot use the adopted-gate route.

## Task 2: Add explicit restore-required retry acknowledgement

**Files:**

- Modify `app/routers/operator.py`.
- Modify `app/controlplane/promotion.py` if event-note helpers are needed.
- Modify `tests/test_release_promotion.py`.
- Modify `tests/test_controlplane.py` only for shared fixtures/contracts.

**Steps:**

1. Replace the generic retry request with a model containing `note` and
   `ack_restore_required`.
2. Extend `_dispatch_development_candidate()` with explicit acknowledgement and
   review-note inputs; default both to fail-closed values for automatic dispatch.
3. Pass the acknowledgement to `plan_update()` and the exact `RolloutRun` persisted
   on `promotion.dev_rollout_id`.
4. Require a non-empty review note when acknowledging a restore-required release.
5. Record the actor, stable acknowledgement fact, linked rollout ID, and sanitized
   note in the promotion audit event.
6. Prove automatic registration remains blocked, explicit acknowledged retry passes
   the plan when backup-ready, and unacknowledged/manual parallel paths cannot
   advance the promotion.

## Task 3: Carry the required secrets epoch into rollout evidence

**Files:**

- Modify `app/controlplane/rollout_exec.py`.
- Modify `app/routers/operator.py`.
- Modify `app/controlplane/pull_reconcile.py`.
- Modify `tests/test_rollout_exec.py` and `tests/test_release_promotion.py`.

**Steps:**

1. Return the expected encrypted-bundle `secrets_epoch` from adopted-gate target
   eligibility.
2. Persist that epoch with `target_source` in the claimed rollout request payload.
3. Keep a candidate queued while the fresh heartbeat has not applied the epoch.
4. During reconciliation, require the promotion-linked heartbeat to report an
   applied epoch at least as high as the persisted requirement.
5. Test missing, malformed, stale, and matching epoch paths without exposing bundle
   plaintext.

## Task 4: Verify the exact full-stack report independently of legacy rows

**Files:**

- Modify `app/controlplane/promotion.py`.
- Modify `app/controlplane/pull_reconcile.py`.
- Modify `app/fleet/heartbeat.py` only if a shared normalized-report helper belongs
  there.
- Modify `tests/test_release_promotion.py` and `tests/test_pull_reconcile.py`.

**Steps:**

1. Build a normalized reported-module map from the OneBrain identity plus module
   health reports.
2. Reject duplicate IDs before converting to a map.
3. For a designated development-gate promotion, compare the normalized map against
   the exact eight-service target release, not only the pre-existing active rows.
4. Require every service to be healthy and at the release's expected version.
5. Reject missing, extra, duplicate, unhealthy, and wrong-version services.
6. Preserve existing customer reconciliation semantics outside the designated gate.

## Task 5: Atomically activate verified module rows

**Files:**

- Modify `app/controlplane/base.py`.
- Modify `app/controlplane/memory.py`.
- Modify `app/controlplane/postgres.py`.
- Modify `app/controlplane/pull_reconcile.py` and `app/controlplane/promotion.py`.
- Modify `tests/test_controlplane.py`, `tests/test_postgres_schema_validation.py`,
  and reconciliation tests.

**Steps:**

1. Extend rollout completion with an optional verified module-version map.
2. Validate that map against the rollout's stored target release before mutation.
3. In the memory store, reconcile all eight rows, deployment version/migration, and
   rollout completion under the existing lock and one persistence write.
4. In PostgreSQL, upsert all eight active rows and update deployment/rollout state in
   one transaction.
5. Leave the legacy rows unchanged on any validation or persistence failure.
6. Use the verified map only for the designated gate's authenticated successful
   promotion-linked report; normal rollouts retain existing behavior.
7. Test atomic success and failure for both stores.

## Task 6: Update the Mission Control release UI and API contract

**Files:**

- Modify `onebrain-web/src/lib/onebrain-client.ts`.
- Modify `onebrain-web/src/lib/onebrain-types.ts`.
- Modify `onebrain-web/src/components/operator-panel.tsx`.
- Modify relevant frontend tests.
- Regenerate `onebrain-web/src/lib/openapi.json`.

**Steps:**

1. Send `ack_restore_required` and the review note from retry actions.
2. For a failed restore-required release, show an unchecked acknowledgement control
   describing the backup/restore consequence.
3. Keep Retry disabled until acknowledgement and a review note are present.
4. Reset acknowledgement state after completion or release selection changes.
5. Preserve accessible labels, existing visual language, and no new dependency.
6. Verify typecheck, lint, tests, production build, and OpenAPI consistency.

## Task 7: Focused and full verification

**Backend focused checks:**

```powershell
py -m pytest -q tests/test_development_gate.py tests/test_rollout_exec.py tests/test_gate_adoption.py
py -m pytest -q tests/test_release_promotion.py tests/test_pull_reconcile.py tests/test_controlplane.py
py -m pytest -q tests/test_postgres_schema_validation.py tests/test_fleet.py
```

**Frontend checks:**

```powershell
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

Run frontend commands from `onebrain-web` using the repository's configured runtime.

**Full checks:**

```powershell
py -m pytest -q
git diff --check
git status --short
```

Also run the existing workflow/secret checks and PostgreSQL migration boundary used
by CI. Inspect the final diff for secrets, mutable image tags, customer-scope changes,
and unrelated files.

## Task 8: Ship PR #19

1. Commit only task-related files on `codex/dev-gate-adoption`.
2. Push the branch and wait for all required checks.
3. Resolve only actionable current review threads.
4. Confirm the four immutable Assistant/Communication repository variables still
   match their green source revisions and digest-pinned images.
5. Re-enable `ONEBRAIN_RELEASE_CANDIDATE_ENABLED` only immediately before the safe
   merge/release run.
6. Merge PR #19 to `main` through the protected branch workflow.
7. Monitor image publication and registration of a new exact eight-service candidate.

## Task 9: Development activation and production boundary

1. Upgrade Mission Control through its separately approved deployment procedure so
   it runs the adoption/acknowledgement code.
2. Run **Prepare existing server** and wait for a fresh heartbeat proving the exact
   secrets epoch.
3. Confirm a fresh successful development-gate backup.
4. Explicitly acknowledge `restore_required` with an audit note and retry the new
   candidate.
5. Verify `target_source=enrolled_development_gate`, exact linked attempt ID, release,
   migration, eight module versions, secrets epoch, and health through
   `dev_verified`.
6. Confirm no replacement server and no customer rollout were created.
7. Stop for the exact offline production signature.
8. After signature verification and explicit approval, roll out only to an explicitly
   selected customer deployment. If no customer exists, report that production has no
   target rather than provisioning one implicitly.

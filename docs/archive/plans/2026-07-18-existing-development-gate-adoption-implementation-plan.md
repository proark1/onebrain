# Existing Development Gate Adoption: Implementation Plan

**Goal:** Reuse an enrolled full-stack `onebrain_development_gate` for development validation, while requiring an explicit provisioned replacement for a legacy Core-only gate and preserving customer rollout safeguards.

**Architecture:** Add a narrow rollout-target eligibility result that accepts either a real successful provisioning run or the currently designated, enrolled, fresh, healthy development gate. Require the active and target module sets to be the exact eight-service topology; return `development_gate_replacement_required` for a legacy Core-only host. Use the existing development-gate provisioner to create and verify that replacement. Keep the admin-only, idempotent preparation operation for a designated full-stack gate's encrypted bundle. Extend candidate registration to carry all eight digest-pinned service entries. Preserve the existing signed desired-state and heartbeat reconciliation state machines.

**Working tree:** `codex/dev-gate-promotion-repair` in `.codex-worktrees/dev-gate-adoption`.

## Task 1: Model adopted pull-target eligibility

**Files:**

- Modify `app/controlplane/rollout_exec.py`
- Modify `tests/test_rollout_exec.py`

**Steps:**

1. Add failing tests for a structured eligibility result with `provider`, `source`, and `reason`.
2. Preserve successful provisioning-run resolution as `source="provisioning_run"`.
3. Add the alternative `source="enrolled_development_gate"` only when the deployment is the designated gate, is `development`/`dedicated_server`, has an active fleet key, and has a fresh healthy heartbeat.
4. Reject customer, undesignated, stale, unhealthy, missing-key, inactive-key, and mismatched-key cases with stable reason codes.
5. Keep `resolve_provisioned_target` as the compatibility boundary for callers that need persisted coordinates; do not synthesize a `target_id`.
6. Run `pytest -q tests/test_rollout_exec.py`.

## Task 2: Use eligibility in rollout dispatch and record its source

**Files:**

- Modify `app/routers/operator.py`
- Modify `tests/test_fleet_orchestration.py`
- Modify `tests/test_release_promotion.py`
- Modify `tests/test_controlplane.py` if shared fixtures require the new stores

**Steps:**

1. Add failing tests proving single and development-candidate dispatch accept the enrolled designated gate but still reject an equivalent customer deployment.
2. Route both direct dispatch and fleet child dispatch through the new eligibility function.
3. Pass the control store, provisioning store, fleet store, settings freshness limit, and current time explicitly enough for deterministic unit tests.
4. Extend `offer_pull_target` to persist `target_source` alongside `provider="hetzner"` and `pull=true`.
5. Preserve rollout claiming, concurrency, update-plan, callback validation, and terminal failure behavior.
6. Run the focused operator, fleet-orchestration, and release-promotion tests.

## Task 3: Reconcile the designated full-stack gate's secret bundle

**Files:**

- Add `app/provisioning/gate_adoption.py`
- Modify `app/routers/operator.py`
- Modify `app/routers/fleet.py` only if a shared epoch/status response helper is needed
- Modify `app/provisioning/service.py` only to expose reusable integration-key metadata helpers
- Add `tests/test_gate_adoption.py`
- Modify `tests/test_release_promotion.py`

**Steps:**

1. Add failing tests for an admin-only, operator-surface endpoint such as `POST /api/operator/development-gate/prepare-existing`.
2. Require the current designated gate, `development`/`dedicated_server` shape, an existing encrypted bundle, and the active-signer-in-served-set interlock.
3. Open the bundle only in MC memory and return no raw secret material.
4. Reconcile canonical gate account/space/app metadata with conflict-safe bootstrap upserts.
5. For each missing Assistant or Communication raw bundle credential:
   - rotate the matching active service key when one exists, revoking the old hash-only key; or
   - create a new least-privilege app-scoped key when none exists;
   - store only the new hash in the service-key store and the one-time plaintext in the encrypted box bundle.
6. Backfill any missing runtime DB/password fields through the existing helper.
7. Validate the complete bundle, reseal it, persist it, and bump `secrets_epoch` only after all steps succeed.
8. Make repeat calls idempotent once the bundle contains valid distinct Assistant and Communication keys.
9. Return only `deployment_id`, `updated`, `secrets_epoch`, and readiness/status fields.
10. Gate candidate dispatch until a fresh healthy heartbeat reports `applied_secrets_epoch >= expected_epoch`.
11. Test atomic failure, no plaintext leakage, idempotency, distinct keys, epoch behavior, and stale applied-epoch blocking.

## Task 4: Register a complete full-stack development candidate

**Files:**

- Modify `scripts/register_release_candidate.py`
- Modify `.github/workflows/tests.yml`
- Modify `tests/test_register_release_candidate.py`
- Update `.env.example` only if local/manual candidate registration documents these non-secret inputs

**Steps:**

1. Add failing tests for the five external module entries and refusal of missing, tag-based, malformed, or partial external refs.
2. Accept non-secret immutable inputs for:
   - Assistant image ref and source revision;
   - shared Communication image ref and source revision.
3. Map the Assistant ref to `assistant-service`.
4. Map the shared Communication ref to `communication-api`, `communication-widget`, `communication-voice`, and `communication-workers`.
5. Preserve the three Core digest inputs and set module versions to their owning source revisions/version.
6. Require the final `modules` and `images` maps to cover exactly the eight development-gate services.
7. Pass the immutable external refs/revisions from repository or `release-dev` environment variables; never add a production private key.
8. Keep the prepare-then-development-sign-then-register flow unchanged.
9. Run the registration tests and OpenAPI drift check if the operator response schema changes.

## Task 5: Expose understandable MC status and actions

**Files:**

- Modify `onebrain-web/src/lib/onebrain-client.ts`
- Modify `onebrain-web/src/lib/onebrain-types.ts`
- Modify `onebrain-web/src/components/operator-panel.tsx`
- Modify relevant frontend tests in `onebrain-web/tests/`
- Regenerate `onebrain-web/src/lib/openapi.json` if the new endpoint is public

**Steps:**

1. Expose separate actions for preparing a compatible designated gate and explicitly provisioning a Core-only gate replacement.
2. Explain the eligibility blockers and applied/expected secret epoch without exposing credentials.
3. Show whether rollout targeting came from `provisioning_run` or `enrolled_development_gate` in operational details.
4. Keep any Mission Control update action outside this flow; after `dev_verified`, show that operator approval is required.
5. Test loading, success, blocked, idempotent, and error states.
6. Run frontend unit tests, lint, typecheck, and production build.

## Task 6: Full verification and shipment

**Files:** All task-related files only.

**Steps:**

1. Run focused backend and frontend tests after each task.
2. Run the complete Python suite with the repository's configured temporary paths.
3. Run frontend tests, lint, typecheck, build, and OpenAPI check.
4. Run PostgreSQL migration/schema smoke checks if the implementation unexpectedly adds persistence; the preferred design adds no migration.
5. Inspect `git diff --check`, `git status`, and the staged diff for secrets and unrelated changes.
6. Commit implementation changes separately from the design and plan commits.
7. Push the feature branch.
8. Follow `AGENTS.md`: fast-forward `main`, merge the feature branch, and push `main` only when all checks pass and no unrelated changes or conflicts are present.

## Task 7: Full-stack development-gate activation

1. Confirm the designated Core-only gate is blocked with `development_gate_replacement_required` and no replacement deployment already exists.
2. Configure the immutable Assistant and Communication image refs/revisions as non-secret release inputs.
3. Explicitly provision the full-stack replacement, verify its eight-service report, and designate it.
4. Run the preparation action for the designated full-stack gate and retain no raw response secrets.
5. Wait for a fresh healthy heartbeat with the expected applied secrets epoch.
6. Register the full-stack development candidate.
7. Confirm the rollout audit payload records the qualified target source.
8. Monitor exact attempt ID, release, migration, eight module versions, and health through `dev_verified`.
9. Confirm exactly one replacement was created and no customer rollout was created.
10. Stop and ask the operator for explicit approval before updating Mission Control itself.

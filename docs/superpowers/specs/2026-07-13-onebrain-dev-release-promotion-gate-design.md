# OneBrain Development Release Promotion Gate

**Status:** Approved design
**Date:** 2026-07-13

## 1. Purpose

Mission Control at `mc.onlyonebrain.com` needs one permanent development
deployment that receives every green release before any customer can receive it.
The development deployment must run the same customer stack on its own dedicated
Hetzner server. It is not the Mission Control server and it is not edited by hand.

The required delivery path is:

```text
main CI
  -> immutable release candidate
  -> automatic development deployment
  -> successful update report
  -> healthy matching heartbeat
  -> offline production signature
  -> explicit operator approval
  -> explicitly selected customer update
```

The system must make bypassing this path technically impossible through the
normal Mission Control APIs, including single-customer and fleet rollout paths.

## 2. Existing Capabilities to Reuse

The repository already provides most of the required machinery:

- Release manifests with Git SHA, per-module versions, digest-pinned images,
  migration metadata, rollback classification, and signatures.
- Dedicated Hetzner provisioning and customer-equivalent compose stacks.
- The `internal`, `pilot`, `early`, `stable`, and `manual` release rings.
- Per-deployment and fleet rollouts, including named deployment targeting.
- The shared `plan_update` safety boundary.
- Box-side signed desired state, update reporting, backups, migrations, smoke
  checks, and recovery.
- Heartbeats containing reported versions, migrations, modules, health, and
  update outcomes.
- Pause, resume, abort, watchdog, and pull-reconciliation behavior.

This feature extends those components. It does not introduce a second deployment
engine or a second update planner.

## 3. Goals

1. Provision and designate exactly one active development release-gate server.
2. Automatically deploy every green main-branch release candidate to that server.
3. Verify the release from authenticated update results and later ground-truth
   heartbeat data.
4. Require a production signature and explicit operator approval before any
   customer update.
5. Enforce the approval in the shared update planner so no normal update path can
   bypass it.
6. Show both server creation time and current-version installation time.
7. Fail closed on missing, stale, conflicting, or unhealthy state.
8. Preserve the existing box-level backup and recovery protections.

## 4. Non-goals

- A generic configurable development/QA/staging environment pipeline.
- Editing source code directly on the development server.
- Automatically updating customers after approval.
- Fleet-wide automatic rollback.
- Traffic splitting, blue-green deployment, or multiple development canaries.
- Moving the production release private key into Mission Control or CI.
- Using customer content in Mission Control acceptance tests.

## 5. Development Deployment

Mission Control designates at most one active deployment as the release gate.
The deployment must satisfy all of these invariants:

- `deployment_type = dedicated_server`
- `environment = development`
- `release_ring = internal`
- effective update policy is `auto`
- `is_release_gate = true`
- the deployment is active

`control_deployments` gains:

```text
is_release_gate BOOLEAN NOT NULL DEFAULT false
current_version_deployed_at TIMESTAMPTZ NULL
```

A partial unique index permits at most one active `is_release_gate` deployment.
Application validation enforces the remaining field invariants in both memory and
Postgres stores.

The operator console exposes a dedicated **Development gate** card. If no gate
exists, the card offers **Provision development server**. Provisioning reuses the
existing Hetzner path and creates a synthetic-data, customer-equivalent stack.
The server is assigned to an internal OneBrain development account and receives a
normal one-time owner credential for testing the customer experience.

Replacing the gate is explicit. Mission Control first requires the replacement to
be active, enrolled, reporting a fresh healthy heartbeat, and running a trusted
baseline version. The old marker and new marker change in one transaction. A
partial handover cannot leave two active gates.

If no healthy gate exists, CI candidates are recorded but cannot be dispatched,
verified, or approved for customers.

## 6. Release Artifact and Promotion State

Release manifest content remains immutable. Mutable lifecycle data lives in a new
table rather than overloading the manifest:

```text
control_release_promotions (
  release_version TEXT PRIMARY KEY REFERENCES control_release_manifests(version),
  gate_deployment_id TEXT NULL REFERENCES control_deployments(id),
  state TEXT NOT NULL,
  dev_signature TEXT NOT NULL,
  dev_signing_key_id TEXT NOT NULL,
  dev_rollout_id TEXT NULL,
  failure_reason TEXT NOT NULL DEFAULT '',
  dev_started_at TIMESTAMPTZ NULL,
  dev_completed_at TIMESTAMPTZ NULL,
  dev_verified_at TIMESTAMPTZ NULL,
  customer_approved_at TIMESTAMPTZ NULL,
  customer_approved_by TEXT NOT NULL DEFAULT '',
  customer_paused_at TIMESTAMPTZ NULL,
  customer_paused_reason TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

Every transition also inserts an immutable row in
`control_release_promotion_events` in the same database transaction. The event
stores the release version, actor, action, previous and next state, review note,
sanitized metadata, and timestamp. Database triggers reject update, delete, and
truncate operations on this event table. This keeps the safety decision and its
audit evidence atomic without coordinating two independent stores.

Allowed states are:

```text
dev_pending
dev_deploying
dev_failed
dev_verified
customer_approved
customer_paused
yanked
```

The main path is:

```text
dev_pending -> dev_deploying -> dev_verified -> customer_approved
```

Failure paths are:

```text
dev_deploying -> dev_failed
dev_failed -> dev_deploying          # explicit safe retry
customer_approved -> customer_paused # customer rollout failure or operator pause
customer_paused -> customer_approved # explicit operator resume after review
any non-yanked state -> yanked
```

Transitions use compare-and-set semantics in Postgres and a lock in the memory
store. Repeated candidate deliveries, callbacks, heartbeats, or reconciliation
ticks are idempotent. A transition from an unexpected state is rejected rather
than guessed.

## 7. Signing and Trust Separation

Automatic development delivery must not weaken customer release trust.

Two signing identities are used:

1. **Development signing key.** Stored in a tightly scoped GitHub Environment and
   trusted only by the development gate server. It signs release candidates after
   the required CI jobs and image publishing succeed.
2. **Production release key.** Kept offline as today and trusted by customer
   servers. It is never stored in GitHub Actions, Mission Control, or the browser.

The CI candidate endpoint verifies the development signature using a configured
development public key. It cannot accept a production approval.

After development verification, the operator signs the exact immutable manifest
with the existing offline CLI. Mission Control accepts the resulting signed
manifest or signature, verifies it against the configured production public key,
and checks that every signed field exactly matches the stored candidate. A changed
Git SHA, image digest, module version, migration field, or rollback classification
is a conflict and requires a new release version.

The release manifest's existing production `signature` and `signing_key_id` fields
remain the customer trust source. The development signature remains in the
promotion record and is never served to customer deployments.

## 8. CI and Automatic Development Delivery

The existing test workflow remains the first gate. Its successful main-branch
path already publishes digest-addressed images. A final job will:

1. Derive the immutable version as `YYYY.MM.DD.<github_run_number>`. A rerun keeps
   the same version and is therefore idempotent; separate green runs on the same
   day cannot collide.
2. Assemble a complete candidate from the exact Git SHA, module versions, image
   digests, migration delta, and rollback classification produced by that run.
   Modules not rebuilt in this repository carry forward their immutable values
   from the previous approved manifest. The first bootstrap uses the manually
   verified trusted baseline manifest.
3. Sign it with the development key.
4. POST it to the narrowly scoped Mission Control candidate endpoint.
5. Treat an identical existing candidate as success.
6. Treat reuse of the version with different immutable content as a hard failure.

The CI machine credential can only create candidates. It cannot designate the
gate, upload production signatures, approve releases, start customer rollouts,
pause/resume customer delivery, or yank releases.

Candidate creation records `dev_pending`. Mission Control then creates a rollout
targeting only the designated gate deployment and moves the candidate to
`dev_deploying`. It reuses the existing named-deployment rollout and Hetzner pull
path. The gate deployment ID is bound to the promotion at dispatch time. No
customer deployment is part of this automatic rollout.

If Mission Control is temporarily unavailable, the CI job fails visibly and can be
rerun safely. If candidate creation succeeds but dispatch fails, the promotion
becomes `dev_failed` with a sanitized reason and can be retried from Mission
Control without rebuilding the images.

## 9. Development Verification

A successful rollout callback or update report is necessary but not sufficient.
Mission Control marks a candidate `dev_verified` only after a heartbeat received
after the rollout reports all of the following:

- The deployment is healthy.
- The reported release version equals the candidate version.
- The reported migration equals the manifest target migration when specified.
- Every enabled module reports the expected version or digest.
- The update attempt ID matches the dev rollout.
- The update outcome is `succeeded`.
- No rollback, timeout, or stale-heartbeat condition is active.

The verification transition records `dev_completed_at` and `dev_verified_at`.
Mismatch, rollback, unhealthy state, or convergence timeout produces `dev_failed`.
The failure does not mutate the previous trusted version and cannot enable the
approval action.

## 10. Production Signature and Operator Approval

The Releases view enables production-signature upload only after
`dev_verified`. Repeated submission of the same valid signature is an idempotent
success.

**Approve for customers** is enabled only when:

- The candidate is `dev_verified`.
- The immutable manifest has a valid production signature.
- The manifest is not yanked.
- The development verification still refers to the current designated gate.

Approval is an explicit authenticated operator action. It records the operator and
timestamp, appends an immutable promotion event, activates the release
manifest for customer use, and moves the promotion to `customer_approved` in one
transaction. Approval does not start any customer rollout.

## 11. Shared Update-Plan Enforcement

`compute_update_plan` remains the authoritative safety boundary for memory and
Postgres implementations.

For the designated development gate, it permits its own `dev_pending` candidate
through the development-signature path. For every other deployment, it requires:

- Promotion state is `customer_approved`.
- The production signature is present and valid. Promotion makes this mandatory
  for customer deployments even if the legacy global signature flag is disabled.
- The release is not yanked.
- The release covers every installed active module.
- The deployment is not pinned to another version.
- A current successful backup exists when migration or rollback classification
  requires it.
- The deployment has a healthy heartbeat received within the greater of two
  configured heartbeat intervals or ten minutes.
- No other rollout is active for the deployment.

The planner returns stable machine-readable denial reasons, including:

```text
release_not_dev_verified
release_not_customer_approved
release_customer_paused
release_yanked
development_gate_missing
development_gate_mismatch
deployment_heartbeat_stale
deployment_unhealthy
backup_required_for_schema_update
```

Single-customer, named-set, manual, and fleet rollouts all call this planner. UI
filtering improves usability but is never treated as enforcement.

Draft releases that are not the current development candidate are not deployable.
This closes the current gap where planner status handling only explicitly rejects
yanked releases.

## 12. Customer Failure Freeze

If an authenticated customer update reports failure, rollback, health failure, or
convergence timeout, its fleet rollout already halts according to the existing
rules. In addition, Mission Control atomically moves that release from
`customer_approved` to `customer_paused`.

While paused, the shared planner rejects new customer updates for that release,
including manual updates outside the original fleet rollout. This favors a small
false-positive pause over spreading a potentially bad release.

Customer fleet rollouts are sequential. When promotion enforcement is enabled,
Mission Control fixes `ring_batch_size` to `1` and `failure_tolerance` to `0` for
non-development deployments. It persists the batch size and explicit target set on
the fleet rollout so a Mission Control restart cannot degrade to the legacy
whole-ring default. The next customer is not offered the release until the current
customer reports terminal success through the normal authenticated path. The
development rollout is already a single-target rollout.

Resuming requires an authenticated operator action with a non-empty review note.
The action is audited. The operator yanks the release when the release itself is
bad, and resumes it only after documenting that the failure was specific to the
customer deployment and does not threaten other deployments.

Mission Control never performs an automatic fleet-wide rollback. The affected box
uses its existing local recovery logic, while Mission Control stops further spread
and waits for operator review.

## 13. Dates and Historical Accuracy

Mission Control exposes three distinct dates rather than conflating them:

- **Server created:** `control_deployments.created_at`.
- **Version installed:** `current_version_deployed_at`, changed only after an
  authenticated successful apply of the current registry version.
- **Last seen:** latest heartbeat receipt time.

Initial provisioning sets the version installation time only after successful
provisioning and a matching heartbeat. A rollout start never changes it. A
successful rollout updates `current_version`, module versions, migration, and
`current_version_deployed_at` atomically.

The migration backfills historical installation time from the newest successful
rollout whose target equals `current_version`, using its authenticated
`completed_at`. If none exists, it uses the newest successful provisioning run
whose recorded target equals `current_version`. Rows without either piece of
evidence remain `NULL` and the UI displays **Unknown**.

## 14. Mission Control User Experience

### 14.1 Development gate card

The card shows:

- Provisioning/enrollment state
- Health and last heartbeat
- Registry and reported version
- Server creation and current-version installation dates
- Enabled modules
- Current candidate and rollout progress
- Sanitized failure reason
- Safe retry and explicit replacement actions

### 14.2 Releases

Each release shows:

- Version, Git SHA, creation date, and image/module coverage
- Migration and rollback classification
- Development deployment status
- Verification time
- Production-signature state
- Customer approval or pause state, operator, and timestamp
- Retry, signature upload, approve, pause/resume, and yank actions when valid

### 14.3 Customers and fleet

Each customer row shows:

- Registry and reported versions, with drift highlighted
- Server creation date
- Current-version installation date
- Last heartbeat and health
- Ring and update policy
- Enabled modules

Only customer-approved releases appear in customer target selectors. The update
plan remains visible before dispatch and explains every block.

The production UI removes manual **Backup ok**, **Health ok**, and force-success
controls. Production success, backup, and health signals must come from signed box
reports, heartbeats, or authenticated workflow callbacks. Test fixtures exercise
the underlying APIs directly; these controls are not rendered by the web console
in any environment.

## 15. API Surface

The new routes are:

- `POST /api/operator/release-candidates` — CI-authenticated candidate creation,
  idempotent by version and manifest digest.
- `GET /api/operator/development-gate` — operator gate state.
- `POST /api/operator/development-gate/provision` — provision the dedicated gate.
- `PUT /api/operator/development-gate/{deployment_id}` — transactional designation
  or replacement after readiness validation.
- `POST /api/operator/releases/{version}/retry-dev` — retry a failed dev rollout.
- `POST /api/operator/releases/{version}/production-signature` — upload and verify
  the offline production signature.
- `POST /api/operator/releases/{version}/approve` — approve for customers.
- `POST /api/operator/releases/{version}/pause` — operator safety pause.
- `POST /api/operator/releases/{version}/resume` — reviewed resume with note.
- `POST /api/operator/releases/{version}/yank` — permanent deployment block.

Existing release reads include promotion state and event history. Existing
update-plan and rollout endpoints gain the centralized planner checks; their route
shape does not change.

CI and heartbeat credentials cannot call operator mutations. Customer principals
cannot read or mutate promotion state. Customer-serving deployments continue to
hide the operator surface.

## 16. Error Handling and Concurrency

- Duplicate identical candidates are successful no-ops.
- Conflicting candidate content for an existing version returns conflict.
- Duplicate terminal callbacks cannot change dates or state twice.
- A late success after timeout cannot revive a failed rollout automatically.
- Heartbeats for an old attempt cannot verify a newer candidate.
- Concurrent approval, pause, or yank uses expected-state compare-and-set.
- A release yanked during rollout cannot be newly applied at completion.
- Gate replacement and approval are transactional.
- Store or scheduler exceptions fail closed and raise an operator-visible alert;
  they do not advance promotion state.
- Failure reasons stored in Mission Control remain metadata-only and exclude logs,
  customer content, document names, prompts, and user identifiers.

## 17. Database and Compatibility Rollout

Delivery is staged to avoid combining schema, infrastructure, and enforcement risk:

1. Add the deployment columns, promotion table, store methods, read APIs, and UI.
2. Deploy Mission Control and verify migrations and existing fleet reads.
3. Provision the synthetic development server from the last trusted production
   manifest and wait for healthy enrollment.
4. Designate it as the sole release gate.
5. Configure the development signing public key in Mission Control and the private
   key in the scoped GitHub Environment.
6. Enable CI candidate delivery and complete one end-to-end development rollout.
7. Upload the matching offline production signature and approve the candidate.
8. Enable mandatory customer-promotion enforcement.
9. Run a customer-update plan against a synthetic/non-customer target and verify
   that unapproved versions are rejected and the approved version is selectable.

`ONEBRAIN_RELEASE_PROMOTION_REQUIRED` defaults to `false` for the additive schema
deployment in steps 1–7. In that mode Mission Control calculates and displays the
new denial reasons without enforcing them. Step 8 sets it to `true` on Mission
Control. From then on, missing promotion state fails closed. The setting cannot be
changed through the web console, and no legacy release is silently grandfathered
for a new deployment.

## 18. Verification Strategy

### Unit tests

- All valid and invalid promotion transitions.
- Identical and conflicting candidate idempotency.
- Development-gate invariants and uniqueness.
- Dev-only and customer update-plan branches.
- Production signature field matching.
- Heartbeat timing, attempt, version, migration, module, and health checks.
- Customer failure freeze and reviewed resume.
- Sequential customer dispatch and restart-safe persisted rollout targeting.
- Date mutation only after authenticated success.

### Store and migration tests

- Memory/Postgres behavior parity.
- Partial unique index and transactional gate handover.
- Compare-and-set transition races.
- Historical date backfill and unknown-date behavior.
- Upgrade from the current Alembic head.

### API and authorization tests

- CI credential can create candidates and nothing else.
- Operator can perform promotion actions.
- Customer principals and fleet keys cannot access promotion mutations.
- Customer surfaces do not mount operator routes.
- Every rollout entry point returns the same planner denial.

### Workflow and UI checks

- Python test suite.
- Frontend lint, typecheck, and production build.
- Workflow contract and secret-pattern checks.
- Release selectors contain only approved releases.
- Manual production health/backup/success controls are absent.
- Dates and drift states render correctly.

### End-to-end acceptance

Using only synthetic data on the dedicated development server:

```text
green main CI
-> digest-pinned candidate registered
-> automatic dev rollout
-> signed success report
-> healthy matching heartbeat
-> dev_verified
-> offline production signature accepted
-> explicit operator approval
-> customer update plan becomes allowed
```

Negative acceptance cases verify that a missing dev server, failed dev rollout,
stale heartbeat, mismatched version, missing production signature, paused release,
and yanked release all keep customer updates blocked.

## 19. Completion Criteria

The feature is complete when:

- Exactly one separate dedicated Hetzner development gate is healthy and visible.
- Every green main release candidate is automatically offered only to that gate.
- Verification requires matching ground-truth heartbeat data.
- Production approval requires the offline signature and explicit operator action.
- Every customer rollout path rejects unapproved, paused, and yanked releases.
- Customer and development views show server-created and version-installed dates.
- Production manual success/health/backup shortcuts are removed.
- A customer failure freezes further spread of that release.
- Customer fleet delivery remains one-at-a-time across Mission Control restarts.
- All automated checks and the synthetic end-to-end acceptance flow pass.

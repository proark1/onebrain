# Existing Development Gate Adoption

## Goal

Deploy and validate new OneBrain releases on a designated full-stack development
gate. Mission Control may adopt an existing full-stack gate that predates the
provisioning ledger, but a legacy Core-only gate must be replaced through the
explicit development-gate provisioner before it can receive a candidate.

After a candidate passes development validation, the workflow stops and asks the
operator for explicit approval before Mission Control itself is updated.

## Current Problem

The development gate is enrolled with Mission Control, owns an active fleet key,
and sends recurring authenticated heartbeats. Pull rollout dispatch nevertheless
requires a successful provisioning-run row. The existing gate has no such row, so
dispatch fails with `no successful Hetzner provisioning target` even though the
pull transport never uses a Hetzner server ID.

The existing release candidates also contain only the three Core images. A valid
development gate runs the eight-service full-stack composition, and the legacy gate
may lack the encrypted runtime credentials required by the Assistant and
Communication services.

The first live Drive candidate, `2026.07.18.270`, exposed two additional fail-closed
boundaries. Its schema change is correctly classified `restore_required`, but the
development retry path has no way to record the operator's acknowledgement on the
promotion-linked rollout. After that acknowledgement, the legacy Core-only host
still cannot apply a full-stack candidate: its compose profiles and local reporting
allowlist were fixed when it was provisioned. Treating its database rows as
expandable would dispatch a rollout that can never converge.

## Scope

This change:

- adopts only the currently designated development gate as an enrolled pull target;
- prepares missing development-only credentials in an adopted full-stack gate's
  encrypted bundle;
- registers a development-signed, digest-pinned full-stack candidate;
- records an explicit restore-required acknowledgement on the exact linked rollout;
- requires an exact eight-service active set before candidate dispatch;
- provisions a separately verified full-stack replacement when the designated gate
  is still Core-only;
- rolls that candidate out to the designated full-stack server and verifies its
  report; and
- stops for operator approval after development verification.

This change does not:

- replace or delete a Hetzner server implicitly during candidate dispatch;
- invent a provisioning run or Hetzner server ID;
- relax targeting for customer deployments;
- bypass release, image, desired-state, version-floor, or heartbeat verification;
- relabel a restore-required migration as code-only;
- create active module rows without matching authenticated host evidence;
- upload a production private key to Mission Control or CI; or
- update Mission Control itself without a later explicit approval.

Candidate `2026.07.18.270` is immutable and remains failed. The repaired workflow
creates a new eight-service candidate; it does not amend, retry, sign, or promote the
three-service manifest.

## Target Eligibility

Introduce one rollout-target eligibility boundary used by single-deployment and
fleet child dispatch.

The existing provisioning-ledger route remains the normal route. A successful
provisioning run with a `hetzner:` target continues to qualify a deployment.

The alternative `enrolled_development_gate` route qualifies a deployment only when
all of the following are true at dispatch time:

1. the deployment is the control store's currently designated release gate;
2. `environment == "development"`;
3. `deployment_type == "dedicated_server"`;
4. at least one deployment-bound fleet key is active;
5. the latest heartbeat was authenticated through such a key;
6. the heartbeat is healthy; and
7. its received timestamp is no older than the existing development-gate freshness
   limit: `max(600, fleet_report_seconds * 2)` seconds.

Customer deployments never use the alternative route. A customer without a
successful provisioning target continues to fail closed.

The resolver returns a small eligibility result containing the provider, source,
and denial reason. An adopted gate needs no fabricated `target_id`; signed desired
state is fetched by the deployment ID already bound to its fleet key.

For the one-time legacy expansion, target eligibility and post-rollout readiness are
separate checks. Before dispatch, the gate may have exactly the three Core active
module rows, but no foreign or unexpected active rows. The target candidate must
contain exactly the required eight services. A gate that already has eight active
rows must match the same exact set. Any other current/target combination fails
closed. `development_baseline_untrusted` remains visible, but it does not prevent a
development-signed candidate from repairing the designated development gate; it
continues to block production trust and customer rollout.

## Restore-Required Retry

Automatic candidate registration never acknowledges destructive rollback risk. A
`restore_required` candidate therefore enters `dev_failed` with
`restore_required_ack_needed` until an authenticated Mission Control administrator
performs an explicit retry.

The retry request carries `ack_restore_required: true` and a review note. Mission
Control passes the acknowledgement to both `plan_update()` and the internally
created `RolloutRun`; the rollout remains the exact ID stored on the promotion. The
existing successful-backup gate still applies. The audit trail records the actor,
the stable acknowledgement fact, the linked rollout ID, and the review note without
recording secrets.

A retry without the acknowledgement remains blocked. A manual parallel rollout is
not an alternative because it is not linked to the promotion state machine.

## Dispatch and Audit Flow

1. Candidate dispatch runs the existing update plan and rollout concurrency gates.
2. The target resolver first checks the provisioning ledger, then the narrow
   adopted-gate conditions.
3. An eligible gate rollout is offered through the existing pull path.
4. The rollout execution request payload records:
   - `provider: "hetzner"`;
   - `pull: true`; and
   - `target_source: "enrolled_development_gate"` or
     `target_source: "provisioning_run"`.
5. The gate fetches its own signed desired state with its fleet key.
6. The gate verifies the development signature, MC wrapper signature, digest-pinned
   images, deployment scope, and version floor before applying anything.
7. Reconciliation accepts success only when the reported attempt ID, release
   version, migration, expected secrets epoch, exact eight-service module set,
   versions, and health all match.
8. Only after that proof, Mission Control atomically reconciles the deployment's
   module registry to the eight verified active rows and completes the linked rollout
   and promotion.

No new adoption table or synthetic provisioning record is added. The existing
deployment, fleet-key, heartbeat, rollout, promotion-event, and execution-payload
records provide the audit trail.

## In-Place Credential Preparation

Before offering the first full-stack candidate, Mission Control reconciles the
existing gate's encrypted secret bundle for the complete development composition.

The preparation operation:

- is restricted to the designated development gate and an authenticated MC admin;
- resolves the existing gate account, canonical spaces, and installed apps;
- mints only missing Assistant and Communication integration credentials;
- ensures required runtime database credentials and roles exist through the current
  least-privilege credential helpers;
- updates the encrypted bundle atomically;
- increments `secrets_epoch` only after the complete bundle is durable; and
- never returns raw credentials in the response or logs.

The existing gate observes the new epoch through its authenticated fleet channel,
pulls the bundle, writes it atomically, and reports `applied_secrets_epoch`. Candidate
dispatch remains blocked until the latest healthy heartbeat reports the expected
epoch. A partial preparation failure leaves the prior bundle and epoch active.

The operation is idempotent. Repeating it reuses existing valid credentials and does
not create duplicate accounts, spaces, apps, or service keys.

Credential preparation does not create active deployment-module rows and cannot
turn a Core-only host into a full-stack host. The explicit provisioner creates the
replacement topology; its authenticated report is the authority for designation.

## Full-Stack Development Candidate

The release-registration workflow is extended to support the complete development
composition:

- Core retains the three immutable images built by the OneBrain repository.
- Assistant uses an immutable `assistant-service` digest and its source revision.
- Communication maps one immutable shared `communication` image digest and source
  revision to `communication-api`, `communication-widget`, `communication-voice`,
  and `communication-workers`, which select their process at runtime.
- The manifest's `modules` and `images` maps cover exactly the same eight module IDs.
- Every image remains under the existing `ghcr.io/proark1` registry allowlist.
- CI signs the candidate only with the development key already held by the
  `release-dev` environment.

The external immutable refs and source revisions are non-secret release inputs. The
registration script validates digest syntax and refuses a partial full-stack map.
The production signing key remains offline and is not needed to test a development
candidate.

## Verified Full-Stack Module Set

The required deployable set is fixed to:

- `onebrain-api`;
- `onebrain-admin-ui`;
- `onebrain-workers`;
- `assistant-service`;
- `communication-api`;
- `communication-widget`;
- `communication-voice`; and
- `communication-workers`.

Candidate dispatch accepts only that exact full-stack set as both the current and
target topology. A legacy Core-only set returns
`development_gate_replacement_required`; the operator must use the existing
development-gate provisioner, verify the replacement, and designate it first. The
signed desired-state manifest carries all eight immutable image references and exact
per-module versions.

Heartbeat reconciliation validates the target set from the combined OneBrain
identity and module-health report rather than iterating only the pre-existing module
rows. Duplicate, missing, extra, unhealthy, or wrong-version reports keep the
rollout non-terminal and eventually fail through the existing timeout path. Once all
evidence agrees, the module-row reconciliation and rollout completion occur in one
control-plane transaction. A partial database write cannot leave the gate appearing
full-stack.

## Failure Handling

- Missing, disabled, or foreign fleet keys block adoption.
- A stale or unhealthy heartbeat blocks dispatch with a specific reason.
- A secret-bundle preparation failure blocks before any container change.
- A gate that has not reported the expected secrets epoch cannot receive the
  full-stack candidate.
- A restore-required retry without a successful backup and explicit acknowledgement
  is rejected before dispatch.
- A current module set other than exact full-stack is rejected. A legacy Core-only
  gate must be replaced through the development-gate provisioner before promotion.
- A target manifest other than the exact eight-service set is rejected.
- An active rollout keeps later candidates queued.
- Signature, digest, scope, version-floor, attempt-ID, migration, module-version, or
  health mismatches fail through the existing state machines.
- A silent gate reaches the existing convergence timeout and fails explicitly.
- Failure never triggers server creation, replacement, deletion, or a customer
  rollout.

## Mission Control Update Boundary

Development verification is the terminal condition for this workflow. Once the
candidate is `dev_verified`, automation stops and reports the evidence to the
operator. Updating the Mission Control host requires a separate explicit approval.
No prior "bring live" instruction is carried across that approval boundary.

## Verification

### Unit and integration tests

- A provisioned Hetzner deployment remains eligible through its successful run.
- The designated gate is eligible through an active key and fresh healthy heartbeat.
- Adoption rejects a non-gate development deployment.
- Adoption rejects every customer deployment.
- Adoption rejects inactive, missing, or deployment-mismatched keys.
- Adoption rejects stale and unhealthy heartbeats.
- Audit payloads distinguish `provisioning_run` from
  `enrolled_development_gate`.
- Credential preparation is admin-only, gate-only, atomic, and idempotent.
- Credential preparation creates only missing credentials and advances the epoch
  once.
- Dispatch waits for the expected applied epoch.
- Automatic dispatch cannot acknowledge `restore_required`; explicit retry can, and
  persists the acknowledgement on the linked rollout.
- A legacy Core gate receives `development_gate_replacement_required`; exact
  full-stack updates succeed while partial, foreign, and extra sets fail closed.
- Candidate registration requires all eight module and image entries.
- The shared Communication digest is accepted for each of its four module IDs.
- Successful reconciliation verifies the exact attempt, release, migration, all
  eight module versions, secrets epoch, and health before activating missing module
  rows.
- A failed or partial report leaves the module registry unchanged.
- Module activation and rollout completion are atomic in both memory and PostgreSQL
  stores.
- Failure and timeout paths remain terminal and auditable.

### Live acceptance

1. Confirm the Core-only gate is rejected with
   `development_gate_replacement_required` before candidate dispatch.
2. Provision the full-stack replacement, verify its exact eight-service heartbeat,
   and designate it as the release gate.
3. Reconcile its encrypted bundle and observe the expected epoch in a fresh healthy
   heartbeat.
4. Register a new development-signed full-stack candidate; leave
   `2026.07.18.270` failed and unchanged.
5. Verify the fresh backup and explicitly acknowledge `restore_required` on the
   retry.
6. Confirm dispatch records the qualified target source and the
   promotion-linked rollout ID.
7. Confirm the replacement server applies all eight immutable images.
8. Confirm the exact eight-service heartbeat atomically reconciles the module rows
   and moves the rollout and promotion to `dev_verified`.
9. Confirm only the explicit gate replacement was created and no customer rollout
   started.
10. Stop for the offline production signature and explicit approval before any
   customer rollout.

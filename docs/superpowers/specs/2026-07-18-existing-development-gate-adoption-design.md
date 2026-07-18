# Existing Development Gate Adoption

## Goal

Deploy and validate new OneBrain releases on the existing
`onebrain_development_gate` server. Mission Control must not create a replacement
Hetzner server merely because the existing gate predates the provisioning ledger.

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

## Scope

This change:

- adopts only the currently designated development gate as an enrolled pull target;
- prepares missing development-only credentials in the existing encrypted bundle;
- registers a development-signed, digest-pinned full-stack candidate;
- rolls that candidate out to the existing server and verifies its report; and
- stops for operator approval after development verification.

This change does not:

- create, replace, or delete a Hetzner server;
- invent a provisioning run or Hetzner server ID;
- relax targeting for customer deployments;
- bypass release, image, desired-state, version-floor, or heartbeat verification;
- upload a production private key to Mission Control or CI; or
- update Mission Control itself without a later explicit approval.

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
7. The existing reconciliation state machine accepts success only when the reported
   attempt ID, release version, migration, module versions, and health all match.

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

## Failure Handling

- Missing, disabled, or foreign fleet keys block adoption.
- A stale or unhealthy heartbeat blocks dispatch with a specific reason.
- A secret-bundle preparation failure blocks before any container change.
- A gate that has not reported the expected secrets epoch cannot receive the
  full-stack candidate.
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
- Candidate registration requires all eight module and image entries.
- The shared Communication digest is accepted for each of its four module IDs.
- Successful reconciliation verifies the exact attempt, release, migration, module
  versions, secrets epoch, and health.
- Failure and timeout paths remain terminal and auditable.

### Live acceptance

1. Confirm the existing gate remains the only designated development gate.
2. Reconcile its full-stack secret bundle and observe the expected epoch in a fresh
   healthy heartbeat.
3. Register the development-signed full-stack candidate.
4. Confirm dispatch records `target_source=enrolled_development_gate`.
5. Confirm the existing server applies all eight immutable images.
6. Confirm the rollout and promotion reach `dev_verified` with exact report matches.
7. Confirm no Hetzner server was created and no customer rollout started.
8. Stop and request operator approval before updating Mission Control.

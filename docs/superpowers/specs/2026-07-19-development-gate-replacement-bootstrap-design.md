# Development Gate Replacement Bootstrap Trust

**Date:** 2026-07-19
**Status:** Approved for implementation

## Goal

Allow Mission Control to provision the first full-stack development release gate
when every customer-approved baseline still contains only the three Core modules.
The provisioner may use the exact development-signed candidate that failed solely
because the designated legacy gate requires replacement. This exception is only a
development-box bootstrap input; it does not approve, promote, retry, or otherwise
change the candidate.

## Current Deadlock

The existing gate correctly rejects an eight-module candidate with
`development_gate_replacement_required`: a Core-only host cannot add the missing
Compose profiles or expand its local module allowlist through an image update.

The replacement endpoint currently calls `_latest_approved_release()`. The latest
approved manifest contains only the three Core modules, so the endpoint rejects it
as incomplete. The full-stack candidate cannot become approved until a full-stack
gate verifies it, creating a circular dependency.

The live instance demonstrates the intended bootstrap evidence: candidate
`2026.07.18.311` is development-signed, digest-pinned, contains the exact eight
required modules and images, and its latest promotion event is
`dev_preflight_failed` with note `development_gate_replacement_required`. It must
remain `dev_failed` and unavailable to customers.

## Considered Approaches

### Selected: narrow development-candidate bootstrap

Let only the development-gate provisioner fall back to an exact replacement-required
candidate when an approved full-stack baseline is unavailable. Reverify all trust
and topology evidence at provisioning time.

This breaks the circular dependency without changing promotion state or customer
release policy.

### Rejected: upgrade the legacy gate in place

Permitting the Core-only gate to pull the full-stack candidate does not update the
host's fixed Compose profiles, local module allowlist, or missing provision-time
credentials. It could never provide trustworthy eight-module convergence evidence.

### Rejected: production-approve the unverified candidate

Attaching a production signature or customer approval before development
verification would bypass the release gate this workflow is intended to enforce.

## Trust and Selection Policy

General release baseline selection remains unchanged. In particular,
`prepare_candidate()` and every customer provisioning path continue to use only the
existing approved or production-signed baseline rules.

The development-gate provisioner uses this ordered policy:

1. Prefer the normal approved/production-signed baseline when it covers every
   required development-gate module and image.
2. Otherwise, consider a development replacement candidate only when all of the
   following hold at request time:
   - a designated development gate exists;
   - its active module set is exactly the three legacy Core modules;
   - the promotion is currently `dev_failed`;
   - its `gate_deployment_id` equals the designated gate;
   - its stored `failure_reason` is `dev_preflight_failed`;
   - its latest promotion event is `dev_preflight_failed -> dev_failed` with the
     exact note `development_gate_replacement_required`;
   - the manifest's module keys are exactly the required eight-module set;
   - the manifest's image keys are exactly the same eight-module set;
   - every image remains digest-pinned and inside the configured registry allowlist;
   - the stored development signature verifies again against the configured
     development release public key; and
   - the manifest has not been yanked.
3. If more than one candidate qualifies, use the most recently updated one, with
   release version as a deterministic tie-breaker.
4. If no release qualifies, return HTTP 409 without creating a provisioning run,
   server, key, DNS record, or secret bundle.

A historical replacement-required event is insufficient. If a later retry fails
for another reason, the latest event no longer matches and the candidate becomes
ineligible.

## Module Boundary

The pure topology and event-shape checks belong in
`app/controlplane/development_gate.py`. Store traversal, configured-key signature
verification, image allowlist verification, and HTTP error translation remain in
the operator service layer.

The new selector is used only by `POST /api/operator/development-gate/provision`.
No global release selector is weakened, and no second signature or authorization
system is introduced.

The dry-run response identifies whether the selected baseline came from the normal
approved path or the replacement-candidate path. It returns versions and immutable
image references but never signatures, fleet keys, service keys, provider tokens,
or decrypted bundle contents.

## Provisioning and Promotion Lifecycle

The selected manifest is passed through the existing Hetzner development
provisioner. That path still renders the development verifier, requires exact
digest-pinned images, creates the complete module composition, and waits for the
normal authenticated provisioning and heartbeat evidence.

Provisioning does not alter the seed candidate. It remains `dev_failed`, draft, and
customer-ineligible. Once the replacement host is healthy and designated, a new
development-signed build is registered and rolled from the seed version through the
normal promotion-linked rollout. That later candidate, not the bootstrap seed,
provides the first `dev_verified` proof for the replacement gate.

The legacy gate is not deleted automatically. Retirement is a separate operator
action after the replacement is designated and stable.

## Failure Handling

- Missing or invalid development verification configuration fails closed.
- A missing, partial, extra, tag-based, foreign-registry, unsigned, invalidly
  signed, yanked, or differently failed candidate is rejected.
- A non-Core or ambiguous current gate module set is rejected.
- Concurrent calls remain governed by the existing deterministic replacement
  identity and provisioning-run conflict controls.
- A Hetzner, DNS, bootstrap, callback, or heartbeat failure uses the existing
  provisioning failure state and does not designate the new gate.
- No failure path promotes the seed candidate or targets a customer deployment.

## Verification

Unit and endpoint tests cover:

- a complete approved baseline remains preferred;
- a Core-only approved baseline falls back to the exact replacement candidate;
- the development signature is independently reverified;
- missing, extra, non-digest, or foreign-registry images fail;
- partial or extra module maps fail;
- wrong state, gate, failure code, latest event, or event note fails;
- a stale replacement event followed by another failure fails;
- a yanked manifest fails;
- the newest of multiple valid replacement candidates is selected deterministically;
- dry-run reports the baseline source without exposing credentials;
- customer and ordinary candidate-baseline selection remain unchanged; and
- a rejected request creates no provisioning state.

Live acceptance is:

1. Upgrade Mission Control to the reviewed implementation.
2. Run the replacement endpoint in dry-run mode and confirm the exact eight-module
   seed and `development_replacement_candidate` source.
3. Provision the replacement and wait for its authenticated healthy eight-module
   heartbeat.
4. Designate the replacement gate without deleting the legacy gate.
5. Register a new development-signed full-stack candidate and verify a normal
   promotion-linked rollout reaches `dev_verified`.
6. Confirm the seed candidate remains failed and no customer rollout occurred.

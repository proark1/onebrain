# Development Release Promotion Activation

This runbook activates the code path that moves green `main` builds through a
dedicated development server before a customer can receive them. Shipping the
code does **not** create a Hetzner server, install secrets, enable CI delivery, or
turn on the hard customer gate.

## Safety invariants

- The production release private key stays offline. It is never a Mission Control,
  browser, server, or GitHub Actions secret.
- GitHub Environment `release-dev` holds only the development private key and the
  candidate bearer secret.
- Customer rollout targets are explicit deployment IDs, one deployment at a time,
  with zero tolerated failures.
- Missing, stale, unhealthy, paused, mismatched, or unsigned state blocks customer
  delivery once promotion enforcement is enabled.

## 1. Deploy the additive code and migration

Deploy Mission Control with migration `0022_release_promotion_gate`. Leave:

```dotenv
ONEBRAIN_RELEASE_PROMOTION_REQUIRED=false
```

At this stage existing releases retain their previous behavior. Update plans show
promotion problems as warnings, which lets the operator audit the fleet before the
hard gate is enabled.

## 2. Generate separate development credentials

Generate an Ed25519 development keypair with the existing release-signing CLI. Put
the development **public** key on Mission Control and the development **private**
key only in GitHub Environment `release-dev`.

Generate a random candidate bearer secret. Store only its `sha256$...` hash on
Mission Control:

```powershell
py -c "from app.fleet.keys import hash_secret; print(hash_secret('replace-with-random-secret'))"
```

Mission Control configuration:

```dotenv
ONEBRAIN_DEV_RELEASE_VERIFY_PUBLIC_KEY=<development public key>
ONEBRAIN_RELEASE_CANDIDATE_KEY_ID=candidate-ci-v1
ONEBRAIN_RELEASE_CANDIDATE_KEY_HASH=sha256$...
ONEBRAIN_FLEET_RECONCILE_SECONDS=60
```

The development public key is baked only into development boxes; production
customer boxes continue to receive only the production release public key. The
reconcile interval drives rollout and post-deploy verification timeouts.

GitHub Environment `release-dev`:

- Secret `ONEBRAIN_MC_URL`
- Secret `ONEBRAIN_RELEASE_CANDIDATE_KEY_ID`
- Secret `ONEBRAIN_RELEASE_CANDIDATE_SECRET`
- Secret `ONEBRAIN_DEV_RELEASE_PRIVATE_KEY`
- Optional variable `ONEBRAIN_DEV_RELEASE_KEY_ID=dev-ci-v1`

Do not create any production-private-key secret.

## 3. Provision and designate the development server

Provisioning a real server is a billed external action and requires action-time
operator confirmation. Use `POST /api/operator/development-gate/provision` with
`dry_run=true` first, review the result, then repeat with `dry_run=false` only after
confirmation. The endpoint fixes the safety-sensitive fields to:

- account `onebrain-development`
- deployment `onebrain-development-gate`
- environment `development`
- dedicated Hetzner server
- `onebrain_only` bundle
- internal ring
- latest trusted customer-approved baseline

Enroll the server, confirm the baseline version, a successful backup, and a fresh
healthy heartbeat. Then designate it through Mission Control. Designation refuses
an unenrolled or unhealthy deployment.

## 4. Verify one harmless candidate

Leave repository variable `ONEBRAIN_RELEASE_CANDIDATE_ENABLED` unset or `false`
until the server is ready. Set it to `true`, merge a harmless change to `main`, and
confirm this sequence in the Releases view:

1. `dev_pending`
2. `dev_deploying`
3. authenticated rollout success
4. a later healthy heartbeat with the exact version, migration, enabled modules,
   and rollout attempt ID
5. `dev_verified`

Sign the exact stored manifest offline. Upload the finished signature and explicitly
approve the release. Approval must not start a customer rollout.

## 5. Enable the customer gate

Before changing the flag, verify that every intended customer reports a fresh
healthy heartbeat and that an approved target is visible. Then set:

```dotenv
ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true
```

Restart Mission Control and prove both cases:

- an unapproved candidate returns `release_not_customer_approved`;
- the approved release plans successfully for a healthy explicit customer target.

## Recovery

- **Dev rollout failure:** candidate becomes `dev_failed`; fix the gate and use
  **Retry dev**. Customers remain blocked.
- **Heartbeat mismatch or unhealthy gate:** candidate becomes `dev_failed`; do not
  approve it. Replace the gate only after the replacement is enrolled and healthy.
- **Customer rollout failure or rollback:** release becomes `customer_paused`
  globally. Review the affected deployment and use resume only with a written note.
- **Known-bad artifact:** yank the release. Yanking is permanent for deployment
  planning; use the separately signed floor-bump process where revocation must be
  enforced on boxes that already possess the signature.
- **Mission Control unavailable:** boxes hold their last known good release. Rerun
  the idempotent candidate job after Mission Control recovers.
- **Emergency compatibility:** turn promotion enforcement off only as a deliberate
  incident action. This restores report-only warnings; it does not repair or approve
  a release and must be followed by an audit of all rollout events.

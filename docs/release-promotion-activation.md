# Development Release Promotion

The development gate is the only environment that may automatically receive a
candidate release. Customer rollouts always require an explicit Mission Control
decision.

## Safety invariants

- A release is identified by immutable image digests and a verified signature.
- The dev gate runs the `full_stack` suite: OneBrain, AI Communication, and
  Personal Assistant services with dummy data.
- A candidate that fails signature, image, schema, health, or smoke validation
  is not eligible for customer selection.
- A customer update preserves a known-good release and a recovery point before
  any schema-changing deployment.
- No release advances all customers automatically.
- A pull rollout is complete only when the current attempt reports the exact
  expected release, migration, module versions, and healthy state. A stale or
  mismatched report remains pending and then fails.

## Promotion flow

1. CI produces immutable images and a signed descriptor.
2. The dev gate verifies the development signing trust, pulls the descriptor,
   and applies the full service set.
3. Validate every required service, the expected image digests, migrations,
   authenticated product flow, customer-isolation protections, and the exact
   rollout report fields.
4. MC records the candidate as eligible only when those validations succeed.
5. A super-admin chooses one customer and starts a rollout.
6. The customer applies the verified production-trusted descriptor, reports
   sanitized health/version data, and MC records success or failure.
7. On failure, stop and restore the recorded known-good release or database
   recovery point before attempting another rollout. A `restore_required`
   release uses the tested database restore path; a code rollback alone is not
   assumed safe.

## Trust separation

Development candidates are verified with the dev public key and remain limited
to the dev gate. Customer releases are verified with the production public key.
The corresponding private signing keys stay offline and never appear in MC,
the broker, a customer host, or CI logs.

## Operator checklist

Before selecting a customer:

- Confirm the dev gate shows the expected complete digest set.
- Confirm all health/smoke checks passed with dummy data.
- Confirm the target customer, intended release, prior release, and recovery
  path in MC.
- Confirm the rollout scope is exactly one selected customer unless a separate,
  explicit batch decision is recorded.
- After rollout, confirm the customer's reported release and health before
  marking it complete.

## Activation canary and recovery rehearsal

Before enabling real customer creation, and after a material broker, database,
firewall, or release-trust change, complete the following with dummy data:

1. Provision through the private broker and confirm only sanitized metadata
   reaches MC.
2. Apply an explicit update to one customer-shaped canary and verify the
   current rollout attempt, release, migration, enabled modules, and health.
3. Exercise the documented rollback path.
4. Restore a backup into a separate, restricted rehearsal target and record
   recovery-point and recovery-time evidence.
5. Run tenant-isolation negative tests: foreign account/workspace reads fail,
   customer ingress exposes no MC/fleet/operator route, and no customer host
   holds another customer's or the broker's credential.

Failure pauses delivery. It does not expand the rollout, auto-roll back every
customer, or authorize a customer teardown.

See [deployment](deployment.md) and
[Mission Control architecture](mission-control-architecture.md) for the
boundaries that make this safe, and
the [production activation runbook](production-activation-runbook.md) for the
external activation evidence.

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

## Promotion flow

1. CI produces immutable images and a signed descriptor.
2. The dev gate verifies the development signing trust, pulls the descriptor,
   and applies the full service set.
3. Validate every required service, the expected image digests, migrations,
   authenticated product flow, and customer-isolation protections.
4. MC records the candidate as eligible only when those validations succeed.
5. A super-admin chooses one customer and starts a rollout.
6. The customer applies the verified production-trusted descriptor, reports
   sanitized health/version data, and MC records success or failure.
7. On failure, stop and restore the recorded known-good release or database
   recovery point before attempting another rollout.

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

See [deployment](deployment.md) and [Mission Control architecture]
(mission-control-architecture.md) for the boundaries that make this safe.

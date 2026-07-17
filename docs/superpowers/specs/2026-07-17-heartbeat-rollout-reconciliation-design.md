# Heartbeat Rollout Reconciliation

## Problem

Hetzner (pull) deployments apply a signed desired state locally and return their
result in an authenticated fleet heartbeat. Mission Control currently persists that
report but only converts it into a terminal rollout state when an administrator
calls the separate reconciliation endpoint. A successful or failed pull rollout can
therefore remain `updating` indefinitely even while Fleet reports a healthy box.

## Goal

Mission Control will reconcile outstanding pull rollouts, including the designated
development gate, every 60 seconds. The control-plane status must reach a terminal,
auditable state without an operator needing to remember a hidden recovery action.

## Chosen approach

Enable Mission Control's existing pull-reconcile scheduler with
`ONEBRAIN_FLEET_RECONCILE_SECONDS=60`, then restart Mission Control.

The scheduler already runs the authoritative `reconcile_pull_targets` reducer
against the latest authenticated heartbeat data. It uses the existing rollout state
machine, including its exact attempt-ID checks and convergence timeout, rather than
adding a second heartbeat-only completion path.

This is preferred over a new heartbeat hook because the scheduler also resolves a
silent box after its deadline. It is preferred over manual-only reconciliation
because the latter is the direct cause of an indefinitely stuck status.

## Data flow

1. A Hetzner box fetches and verifies a signed desired state, applies it, and sends
   a heartbeat containing its attempt ID and outcome.
2. Mission Control persists the heartbeat and its telemetry as it does today.
3. Every 60 seconds, Mission Control runs `reconcile_pull_targets` using the latest
   heartbeat set. Only pull-marked, non-terminal rollout rows can change state.
4. A matching `succeeded` report marks the rollout successful through the existing
   update-plan gate; a matching failure or an expired convergence deadline marks it
   failed with the existing reason codes.
5. The next gate heartbeat promotes a successful development rollout to
   `dev_verified` only when its version, migration, module versions, attempt ID,
   and health match the signed release.
6. The normal pending-candidate dispatcher may then select the next candidate. It
   never automatically retries a failed candidate.

## Safety boundaries

- The change does not alter release signatures, desired-state verification, image
  pinning, backup gates, or health checks.
- A box can only resolve the rollout whose exact attempt ID it reports.
- Terminal rollouts are never reopened.
- The existing deadline still turns silent or in-progress pull attempts into an
  explicit failure, even if the box sends no further heartbeat.
- Railway rollout callbacks remain unchanged.

## Current stuck rollout

The currently deployed Mission Control version already exposes both the
authenticated manual reconciliation endpoint and the scheduler code, but the live
environment has not opted into the scheduler. Recovery has two bounded operations:

1. Call `POST /api/operator/fleet-rollouts/reconcile` once as an administrator.
   This turns the current target into either a verified completion or an explicit
   failure based on the gate's authenticated report; it does not force a version.
2. Set `ONEBRAIN_FLEET_RECONCILE_SECONDS=60` on Mission Control and restart it.
   The scheduler then prevents the same indefinite state on later releases.

This deliberately does not edit the production database or bypass operator
authentication. After a successful reconciliation, the normal candidate flow can
continue; after a failure, the UI can present the concrete failure instead of a
permanent `updating` badge.

## Verification

The scheduler and pull-reducer behavior already have focused regression tests. Before
the configuration change, verify that the live Mission Control instance starts with
operator mode enabled, set the 60-second interval, and restart it. Confirm the
startup log says the scheduler is enabled. Then reconcile the stuck rollout once and
confirm that it becomes either completed or failed within one scheduler interval;
the next gate heartbeat completes development verification when the report matches.

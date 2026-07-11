"""Fleet rollout runner — drives a fleet rollout ring by ring.

Wires the pure planner/reducer (orchestration.py) to the Phase-1 per-deployment
executor. `dispatch_child(fleet_id, deployment_id)` — which creates and dispatches
one child rollout — is INJECTED, so the ring-advance logic here is unit-testable
with a fake dispatcher (the real one is the operator endpoint's infra tail).

Advancement is callback-driven: when a child rollout reaches a terminal status,
the callback router calls advance_fleet_on_child, which reconciles the parent and
either pauses (too many failures), opens the next ring, or completes.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from app.controlplane.orchestration import (
    FleetRolloutPlan,
    FleetRolloutRun,
    advance_fleet_rollout,
    plan_fleet_rollout,
)


def _deployments_in_ring(control_store, ring: str, target_version: str) -> List[str]:
    """Deployments in `ring` that still need `target_version` (re-planned now, so a
    schema-changing update re-checks its backup at dispatch time, not just at plan)."""
    ids = []
    for dep in control_store.list_deployments():
        if dep.release_ring != ring:
            continue
        plan = control_store.plan_update(dep.id, target_version)
        if plan.allowed and plan.reason != "already_current":
            ids.append(dep.id)
    return sorted(ids)


def plan_and_start_fleet_rollout(
    control_store, fleet_store, *, fleet_id: str, target_version: str, git_sha: str,
    failure_tolerance: int, started_by: str, created_at: str, callback_url: str = "",
    dry_run: bool = True, dispatch_child: Callable,
) -> Tuple[Optional[FleetRolloutRun], FleetRolloutPlan]:
    """Plan the sweep, persist the fleet rollout, and dispatch its FIRST ring.
    Returns (fleet_run|None, plan). fleet_run is None when nothing is deployable
    (everything already-current or blocked) — the caller surfaces plan.blocked.
    callback_url/dry_run are persisted so later rings dispatch the same way."""
    plan = plan_fleet_rollout(control_store.list_deployments(), target_version, control_store.plan_update)
    if not plan.deployable:
        return None, plan
    fleet_run = fleet_store.create_fleet_rollout(FleetRolloutRun(
        id=fleet_id, target_version=target_version, git_sha=git_sha, status="running",
        ring_order=plan.ring_order, current_ring=plan.ring_order[0],
        failure_tolerance=failure_tolerance, started_by=started_by, created_at=created_at,
        callback_url=callback_url, dry_run=dry_run,
    ))
    for deployment_id in plan.waves[0].deployment_ids:
        dispatch_child(fleet_run, deployment_id)
    # Reconcile now: a child that failed synchronously at dispatch never posts a
    # callback, so if the WHOLE first ring dispatch-fails there is nothing to wake
    # the reducer later. reconcile is a no-op when children are genuinely in-flight.
    reconciled = reconcile_fleet_rollout(control_store, fleet_store, fleet_id, dispatch_child=dispatch_child)
    return (reconciled or fleet_run), plan


def _current_ring_children(control_store, fleet_run: FleetRolloutRun):
    children = []
    for child in control_store.list_rollouts_for_fleet(fleet_run.id):
        deployment = control_store.get_deployment(child.deployment_id)
        if deployment and deployment.release_ring == fleet_run.current_ring:
            children.append(child)
    return children


def reconcile_fleet_rollout(control_store, fleet_store, fleet_id: str, *, dispatch_child: Callable) -> Optional[FleetRolloutRun]:
    """Evaluate the current ring and pause / advance / complete. Idempotent and safe
    to call on every child callback, on resume, and after any dispatch.

    Loops (rather than waits on a callback) so that a ring whose children are ALL
    already terminal — because they failed synchronously at dispatch, or the ring
    turned out empty — advances/pauses immediately instead of hanging at 'running'.
    The ring transition is an atomic compare-and-set (advance_fleet_ring), so
    concurrent child callbacks can't both open the next ring."""
    while True:
        fleet_run = fleet_store.get_fleet_rollout(fleet_id)
        if not fleet_run or fleet_run.status != "running":
            return fleet_run  # only a running fleet rollout auto-advances
        decision = advance_fleet_rollout(fleet_run, _current_ring_children(control_store, fleet_run))
        if decision.action == "wait":
            return fleet_run
        if decision.action == "pause":
            return fleet_store.update_fleet_rollout(fleet_id, status="paused", notes=decision.reason)
        if decision.action == "succeeded":
            return fleet_store.update_fleet_rollout(fleet_id, status="succeeded")
        # advance: atomically claim the ring transition; if we lose the race another
        # callback already opened the next ring, so do nothing.
        if not fleet_store.advance_fleet_ring(fleet_id, fleet_run.current_ring, decision.next_ring):
            return fleet_store.get_fleet_rollout(fleet_id)
        advanced = fleet_store.get_fleet_rollout(fleet_id)
        for deployment_id in _deployments_in_ring(control_store, decision.next_ring, advanced.target_version):
            dispatch_child(advanced, deployment_id)
        # loop: re-evaluate the ring just opened (children in-flight -> wait; all
        # synchronously terminal / empty -> advance or pause again).


def advance_fleet_on_child(control_store, fleet_store, child_rollout, *, dispatch_child: Callable) -> Optional[FleetRolloutRun]:
    """Callback hook: a child rollout reached terminal — reconcile its parent."""
    fleet_id = getattr(child_rollout, "fleet_rollout_id", "") or ""
    if not fleet_id:
        return None
    return reconcile_fleet_rollout(control_store, fleet_store, fleet_id, dispatch_child=dispatch_child)

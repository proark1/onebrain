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

from app.controlplane.base import effective_update_policy
from app.controlplane.orchestration import (
    FleetRolloutPlan,
    FleetRolloutRun,
    advance_fleet_rollout,
    plan_fleet_rollout,
)


def _deployments_in_ring(control_store, ring: str, target_version: str, *,
                         only_deployment_ids: "frozenset[str]" = frozenset(),
                         include_manual_pinned: bool = False) -> List[str]:
    """Deployments in `ring` that still need `target_version` (re-planned now, so a
    schema-changing update re-checks its backup at dispatch time, not just at plan
    — and a deployment whose policy left "auto" mid-rollout is never re-swept).

    A16: honors the SAME named-set + manual/pinned override as plan_fleet_rollout, so
    the re-dispatch bucket matches the plan bucket in EVERY ring (not just the first).
    A dep is included iff:
      - (only_deployment_ids empty OR dep.id in only_deployment_ids), AND
      - (effective_update_policy(dep)=='auto' OR (include_manual_pinned AND dep.id in
        only_deployment_ids)), AND
      - plan_update allows it and it is not already-current at target_version.
    Defaults reproduce today's unconditional-auto filter exactly."""
    ids = []
    for dep in control_store.list_deployments():
        if dep.release_ring != ring:
            continue
        if only_deployment_ids and dep.id not in only_deployment_ids:
            continue
        if effective_update_policy(dep) != "auto":
            if not (include_manual_pinned and dep.id in only_deployment_ids):
                continue
        plan = control_store.plan_update(dep.id, target_version)
        if plan.allowed and plan.reason != "already_current":
            ids.append(dep.id)
    return sorted(ids)


def _dispatch_ring_batch(control_store, ring: str, target_version: str, *, batch_size: int, inflight: int,
                         dispatch_child: Callable, fleet_run,
                         only_deployment_ids: "frozenset[str]" = frozenset(),
                         include_manual_pinned: bool = False) -> int:
    """Dispatch at most (batch_size - inflight) of the ring's still-pending deployments
    (sorted, deterministic). batch_size<=0 -> dispatch ALL pending (today's behavior).
    'inflight' = current non-terminal children of this ring. Returns how many it
    dispatched. Threads the A16 named-set/manual override to _deployments_in_ring."""
    pending = _deployments_in_ring(control_store, ring, target_version,
                                   only_deployment_ids=only_deployment_ids,
                                   include_manual_pinned=include_manual_pinned)   # excludes already-current
    room = len(pending) if batch_size <= 0 else max(0, batch_size - inflight)
    for deployment_id in pending[:room]:
        dispatch_child(fleet_run, deployment_id)
    return min(room, len(pending))


def plan_and_start_fleet_rollout(
    control_store, fleet_store, *, fleet_id: str, target_version: str, git_sha: str,
    failure_tolerance: int, started_by: str, created_at: str, callback_url: str = "",
    dry_run: bool = True, dispatch_child: Callable, ring_batch_size: int = 0,
    only_deployment_ids: "frozenset[str]" = frozenset(), include_manual_pinned: bool = False,
) -> Tuple[Optional[FleetRolloutRun], FleetRolloutPlan]:
    """Plan the sweep, persist the fleet rollout, and dispatch its FIRST ring.
    Returns (fleet_run|None, plan). fleet_run is None when nothing is deployable
    (everything already-current or blocked) — the caller surfaces plan.blocked.
    callback_url/dry_run are persisted so later rings dispatch the same way.

    P4-07: ring_batch_size + only_deployment_ids + include_manual_pinned are RUNTIME
    call params (never persisted). ring_batch_size<=0 dispatches the whole ring at once
    (today's behavior); >0 caps concurrent in-flight children per ring. The named-set /
    manual override is threaded into both the plan and the batch dispatcher (A16).
    Defaults reproduce today's call exactly."""
    plan = plan_fleet_rollout(control_store.list_deployments(), target_version, control_store.plan_update,
                              only_deployment_ids=only_deployment_ids,
                              include_manual_pinned=include_manual_pinned)
    if not plan.deployable:
        return None, plan
    fleet_run = fleet_store.create_fleet_rollout(FleetRolloutRun(
        id=fleet_id, target_version=target_version, git_sha=git_sha, status="running",
        ring_order=plan.ring_order, current_ring=plan.ring_order[0],
        failure_tolerance=failure_tolerance, started_by=started_by, created_at=created_at, updated_at=created_at,
        callback_url=callback_url, dry_run=dry_run,
        ring_batch_size=ring_batch_size,
        only_deployment_ids=tuple(sorted(only_deployment_ids)),
        include_manual_pinned=include_manual_pinned,
    ))
    # Dispatch the first ring under the batch cap (threading the SAME override as the
    # plan, A16). inflight=0 — no children exist yet.
    _dispatch_ring_batch(control_store, plan.ring_order[0], target_version,
                         batch_size=ring_batch_size, inflight=0, dispatch_child=dispatch_child,
                         fleet_run=fleet_run, only_deployment_ids=only_deployment_ids,
                         include_manual_pinned=include_manual_pinned)
    # Reconcile now: a child that failed synchronously at dispatch never posts a
    # callback, so if the WHOLE first ring dispatch-fails there is nothing to wake
    # the reducer later. reconcile is a no-op when children are genuinely in-flight.
    reconciled = reconcile_fleet_rollout(control_store, fleet_store, fleet_id, dispatch_child=dispatch_child,
                                         ring_batch_size=ring_batch_size,
                                         only_deployment_ids=only_deployment_ids,
                                         include_manual_pinned=include_manual_pinned)
    return (reconciled or fleet_run), plan


def _current_ring_children(control_store, fleet_run: FleetRolloutRun):
    children = []
    for child in control_store.list_rollouts_for_fleet(fleet_run.id):
        deployment = control_store.get_deployment(child.deployment_id)
        if deployment and deployment.release_ring == fleet_run.current_ring:
            children.append(child)
    return children


def _open_next_ring_batch(control_store, fleet_run, children, *, ring_batch_size: int, dispatch_child: Callable,
                          only_deployment_ids: "frozenset[str]", include_manual_pinned: bool) -> bool:
    """Intra-ring staggering. Returns True (and dispatches the next batch) iff the
    current ring's in-flight children are ALL terminal with NO failures and the ring
    still has un-dispatched pending deployments — then it opens the next batch for the
    SAME ring (capped by ring_batch_size; <=0 -> the whole remaining ring at once,
    today's behavior / the not-persisted safe degradation). A failure or an in-flight
    child returns False so advance_fleet_rollout evaluates the ring (pause/advance) at
    DRAIN time — a failure is never re-dispatched, so it still counts toward tolerance
    via the UNCHANGED reducer. Threads the A16 named-set/manual override."""
    if ring_batch_size <= 0:
        # Default path: the whole ring is dispatched at ring-open, so there is no next
        # batch. Return False WITHOUT consulting the store (pre-P4-07 parity) — a
        # deployment that became eligible/unblocked mid-rollout is NOT re-swept into the
        # already-open ring; advance_fleet_rollout advances past it exactly as before.
        return False
    if any(child.status not in {"success", "failed"} for child in children):
        return False  # batch still in flight
    if any(child.status == "failed" for child in children):
        return False  # a failure -> let the reducer decide at drain
    pending = _deployments_in_ring(control_store, fleet_run.current_ring, fleet_run.target_version,
                                   only_deployment_ids=only_deployment_ids,
                                   include_manual_pinned=include_manual_pinned)
    if not pending:
        return False  # ring fully drained -> advance_fleet_rollout evaluates
    _dispatch_ring_batch(control_store, fleet_run.current_ring, fleet_run.target_version,
                         batch_size=ring_batch_size, inflight=0, dispatch_child=dispatch_child,
                         fleet_run=fleet_run, only_deployment_ids=only_deployment_ids,
                         include_manual_pinned=include_manual_pinned)
    return True


def reconcile_fleet_rollout(control_store, fleet_store, fleet_id: str, *, dispatch_child: Callable,
                            ring_batch_size: int = 0, only_deployment_ids: "frozenset[str]" = frozenset(),
                            include_manual_pinned: bool = False) -> Optional[FleetRolloutRun]:
    """Evaluate the current ring and pause / advance / complete. Idempotent and safe
    to call on every child callback, on resume, and after any dispatch.

    Loops (rather than waits on a callback) so that a ring whose children are ALL
    already terminal — because they failed synchronously at dispatch, or the ring
    turned out empty — advances/pauses immediately instead of hanging at 'running'.
    The ring transition is an atomic compare-and-set (advance_fleet_ring), so
    concurrent child callbacks can't both open the next ring.

    P4-07: ring_batch_size + the named-set/manual override are RUNTIME params (not
    persisted). When set, a fully-terminal-no-failure batch with pending work left opens
    the next batch for the SAME ring before advancing (intra-ring staggering). Defaults
    (0 / frozenset() / False) reproduce today's behavior exactly; the callback path
    (advance_fleet_on_child) uses them, so on a mid-rollout MC restart the remaining work
    dispatches unbounded/auto-only — a safe degradation that never skips a ring gate."""
    while True:
        fleet_run = fleet_store.get_fleet_rollout(fleet_id)
        if not fleet_run or fleet_run.status != "running":
            return fleet_run  # only a running fleet rollout auto-advances
        # The safety policy lives on the parent record so callbacks and restart
        # reconciliation cannot widen a batch or forget an explicit target set.
        ring_batch_size = fleet_run.ring_batch_size
        only_deployment_ids = frozenset(fleet_run.only_deployment_ids)
        include_manual_pinned = fleet_run.include_manual_pinned
        children = _current_ring_children(control_store, fleet_run)
        if _open_next_ring_batch(control_store, fleet_run, children, ring_batch_size=ring_batch_size,
                                 dispatch_child=dispatch_child, only_deployment_ids=only_deployment_ids,
                                 include_manual_pinned=include_manual_pinned):
            continue  # the next batch of the SAME ring is now in flight; re-evaluate
        decision = advance_fleet_rollout(fleet_run, children)
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
        _dispatch_ring_batch(control_store, decision.next_ring, advanced.target_version,
                             batch_size=ring_batch_size, inflight=0, dispatch_child=dispatch_child,
                             fleet_run=advanced, only_deployment_ids=only_deployment_ids,
                             include_manual_pinned=include_manual_pinned)
        # loop: re-evaluate the ring just opened (children in-flight -> wait; all
        # synchronously terminal / empty -> advance or pause again).


def advance_fleet_on_child(control_store, fleet_store, child_rollout, *, dispatch_child: Callable) -> Optional[FleetRolloutRun]:
    """Callback hook: a child rollout reached terminal — reconcile its parent."""
    fleet_id = getattr(child_rollout, "fleet_rollout_id", "") or ""
    if not fleet_id:
        return None
    return reconcile_fleet_rollout(control_store, fleet_store, fleet_id, dispatch_child=dispatch_child)

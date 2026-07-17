"""Fleet-wide rollout orchestration — deploy a release across the fleet ring by
ring, containing a bad release to the smallest blast radius.

The core is two PURE functions:
- plan_fleet_rollout: bucket the fleet's deployments into ordered ring waves,
  skipping already-current ones and surfacing blocked ones (backup required,
  release missing modules) as pre-flight failures the operator must clear.
- advance_fleet_rollout: a reducer over the current ring's child rollout statuses
  that decides wait / pause / advance-to-next-ring / succeeded. Failures beyond
  `failure_tolerance` in a ring PAUSE the fleet rollout before the next ring opens,
  so a canary failure in internal/pilot never reaches early/stable.

Both are pure of I/O and the executor, so ring orchestration is unit-testable
without an external workflow executor. The runner that offers each ring's child rollouts
is thin glue over the Phase-1 executor (the noted infra tail).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from app.controlplane.base import effective_update_policy

# The auto-swept rings, in widening order. "manual" is deliberately excluded — a
# manual-ring deployment opts out of fleet rollouts and is never swept.
RING_ORDER: Tuple[str, ...] = ("internal", "pilot", "early", "stable")

FLEET_STATUSES = frozenset({"pending", "running", "paused", "succeeded", "failed", "aborted"})
FLEET_TERMINAL = frozenset({"succeeded", "failed", "aborted"})


@dataclass(frozen=True)
class FleetRolloutRun:
    id: str
    target_version: str
    status: str = "pending"        # pending | running | paused | succeeded | failed | aborted
    ring_order: Tuple[str, ...] = ()   # the rings WITH deployments to update, in order
    current_ring: str = ""             # the ring currently in flight ("" before start)
    failure_tolerance: int = 0         # failures allowed per ring before pausing
    started_by: str = ""
    git_sha: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    # Carried so later rings can be dispatched on advance without re-supplying them.
    callback_url: str = ""             # child callback template (with {rollout_id})
    dry_run: bool = True
    ring_batch_size: int = 1            # persisted; one customer at a time by default
    only_deployment_ids: Tuple[str, ...] = ()
    include_manual_pinned: bool = False


@dataclass(frozen=True)
class RingWave:
    ring: str
    deployment_ids: Tuple[str, ...]


@dataclass(frozen=True)
class FleetRolloutPlan:
    target_version: str
    waves: Tuple[RingWave, ...]           # ring waves that have deployments, in order
    skipped: Tuple[str, ...] = ()         # already-current / no-update deployments
    blocked: Dict[str, str] = field(default_factory=dict)  # deployment_id -> reason

    @property
    def ring_order(self) -> Tuple[str, ...]:
        return tuple(w.ring for w in self.waves)

    @property
    def deployable(self) -> bool:
        return any(w.deployment_ids for w in self.waves)


def plan_fleet_rollout(deployments, target_version: str, plan_for: Callable, ring_order=RING_ORDER, *,
                       only_deployment_ids: "frozenset[str]" = frozenset(),
                       include_manual_pinned: bool = False) -> FleetRolloutPlan:
    """Bucket deployments into ordered ring waves. `plan_for(deployment_id,
    target_version)` is injected (= ControlPlaneStore.plan_update) so this is pure.

    P4-07 targeting (additive; defaults reproduce today's signature exactly):
    - only_deployment_ids: when non-empty, a NAMED SET — a deployment not in it is
      skipped entirely (neither bucketed nor surfaced as blocked).
    - include_manual_pinned: a manual/pinned deployment is normally excluded from
      fleet sweeps; when it is explicitly NAMED and this flag is set, the policy
      exclusion is overridden (a deliberate operator update). The R3 restore_required
      ack still applies at DISPATCH (plan_for) — this override never auto-acks; a
      pinned deployment targeting a different version is still plan-blocked there.
    Whole-ring waves are unchanged (no synthetic ring labels / no chunking here)."""
    by_ring: Dict[str, List[str]] = {ring: [] for ring in ring_order}
    skipped: List[str] = []
    blocked: Dict[str, str] = {}
    for dep in deployments:
        if only_deployment_ids and dep.id not in only_deployment_ids:
            continue  # named-set: only the listed deployments participate
        if effective_update_policy(dep) != "auto":
            # manual/pinned deployments are only swept when explicitly named + overridden
            if not (include_manual_pinned and dep.id in only_deployment_ids):
                continue
        if dep.release_ring not in by_ring:
            continue  # unknown ring: opted out of fleet sweeps
        plan = plan_for(dep.id, target_version)
        if plan.reason == "already_current":
            skipped.append(dep.id)
        elif not plan.allowed:
            blocked[dep.id] = plan.reason
        else:
            by_ring[dep.release_ring].append(dep.id)
    waves = tuple(
        RingWave(ring=ring, deployment_ids=tuple(sorted(by_ring[ring])))
        for ring in ring_order if by_ring[ring]
    )
    return FleetRolloutPlan(
        target_version=target_version, waves=waves,
        skipped=tuple(sorted(skipped)), blocked=dict(blocked),
    )


@dataclass(frozen=True)
class FleetDecision:
    action: str            # wait | pause | advance | succeeded
    next_ring: str = ""
    failures: int = 0
    reason: str = ""


def advance_fleet_rollout(fleet_run: FleetRolloutRun, current_ring_children) -> FleetDecision:
    """Decide what a fleet rollout should do once its current ring's children have
    (or have not yet) all reached a terminal status. Pure reducer."""
    if any(child.status not in {"success", "failed"} for child in current_ring_children):
        return FleetDecision(action="wait")

    failures = sum(1 for child in current_ring_children if child.status == "failed")
    if failures > fleet_run.failure_tolerance:
        return FleetDecision(
            action="pause", failures=failures,
            reason=(f"{failures} failure(s) in ring '{fleet_run.current_ring}' exceed "
                    f"tolerance {fleet_run.failure_tolerance}"),
        )

    rings = list(fleet_run.ring_order)
    idx = rings.index(fleet_run.current_ring) if fleet_run.current_ring in rings else -1
    remaining = rings[idx + 1:]
    if not remaining:
        return FleetDecision(action="succeeded", failures=failures)
    return FleetDecision(action="advance", next_ring=remaining[0], failures=failures)


class FleetRolloutStore:
    """Persistence for fleet rollouts (the parent of per-deployment child rollouts,
    which carry fleet_rollout_id)."""

    def create_fleet_rollout(self, fleet_run: FleetRolloutRun) -> FleetRolloutRun: ...

    def get_fleet_rollout(self, fleet_rollout_id: str) -> Optional[FleetRolloutRun]: ...

    def list_fleet_rollouts(self) -> List[FleetRolloutRun]: ...

    def update_fleet_rollout(self, fleet_rollout_id: str, **fields) -> FleetRolloutRun: ...

    def advance_fleet_ring(self, fleet_rollout_id: str, from_ring: str, to_ring: str) -> bool: ...


FLEET_EXEC_FIELDS = frozenset({"status", "current_ring", "notes", "ring_order"})

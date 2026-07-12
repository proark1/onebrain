"""Fleet rollout orchestration (Phase 2): the pure planner + reducer, the runner
driving a fleet rollout ring by ring with a fake dispatcher, the store, and the
operator fleet-rollout endpoints. Railway-free."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.operator as operator_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest, RolloutRun
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.orchestration import FleetRolloutRun, advance_fleet_rollout, plan_fleet_rollout
from app.controlplane.fleet_runner import (
    _deployments_in_ring,
    advance_fleet_on_child,
    plan_and_start_fleet_rollout,
    reconcile_fleet_rollout,
)
from app.controlplane.rollout_exec import RolloutCallback, apply_rollout_callback


# --- pure planner ------------------------------------------------------------

def _dep(dep_id, ring):
    return SimpleNamespace(id=dep_id, release_ring=ring)


def _plan_for(mapping):
    def plan_for(dep_id, target):
        allowed, reason = mapping.get(dep_id, (True, "update_available"))
        return SimpleNamespace(allowed=allowed, reason=reason)
    return plan_for


def test_plan_buckets_by_ring_skips_current_blocks_and_excludes_manual():
    deps = [_dep("a", "internal"), _dep("b", "pilot"), _dep("c", "internal"),
            _dep("d", "manual"), _dep("e", "stable")]
    plan = plan_fleet_rollout(deps, "v2", _plan_for({
        "c": (True, "already_current"),
        "e": (False, "backup_required_for_schema_update"),
    }))

    assert [w.ring for w in plan.waves] == ["internal", "pilot"]  # stable blocked-only; manual excluded
    assert {w.ring: list(w.deployment_ids) for w in plan.waves} == {"internal": ["a"], "pilot": ["b"]}
    assert plan.skipped == ("c",)
    assert plan.blocked == {"e": "backup_required_for_schema_update"}
    assert plan.deployable is True
    assert plan.ring_order == ("internal", "pilot")


def test_plan_not_deployable_when_all_current():
    plan = plan_fleet_rollout([_dep("a", "pilot")], "v2", _plan_for({"a": (True, "already_current")}))
    assert plan.deployable is False and plan.waves == ()


def test_plan_excludes_manual_and_pinned_policies():
    """WP4: fleet sweeps are auto-policy only. manual/pinned deployments are
    excluded up front — they appear in neither waves nor blocked; '' policy on
    an auto ring keeps the legacy ring convention (dormancy)."""
    deps = [
        SimpleNamespace(id="m", release_ring="pilot", update_policy="manual"),
        SimpleNamespace(id="p", release_ring="pilot", update_policy="pinned"),
        SimpleNamespace(id="a", release_ring="pilot", update_policy="auto"),
        SimpleNamespace(id="e", release_ring="pilot", update_policy=""),
    ]

    plan = plan_fleet_rollout(deps, "v2", _plan_for({}))

    assert {w.ring: list(w.deployment_ids) for w in plan.waves} == {"pilot": ["a", "e"]}
    assert plan.skipped == ()
    assert plan.blocked == {}  # excluded, not surfaced as pre-flight failures


# --- pure reducer ------------------------------------------------------------

def _child(status):
    return SimpleNamespace(status=status)


def _fr(ring_order, current, tol=0):
    return FleetRolloutRun(id="f", target_version="v", status="running",
                           ring_order=ring_order, current_ring=current, failure_tolerance=tol)


def test_advance_waits_pauses_advances_and_completes():
    fr = _fr(("internal", "pilot"), "internal")
    assert advance_fleet_rollout(fr, [_child("running")]).action == "wait"
    assert advance_fleet_rollout(fr, [_child("success"), _child("failed")]).action == "pause"  # 1 > tol 0

    d = advance_fleet_rollout(_fr(("internal", "pilot"), "internal", tol=1),
                              [_child("success"), _child("failed")])
    assert d.action == "advance" and d.next_ring == "pilot"  # 1 <= tol 1

    assert advance_fleet_rollout(_fr(("internal", "pilot"), "pilot"), [_child("success")]).action == "succeeded"


# --- runner: full ring-by-ring progression -----------------------------------

def _fleet_control():
    store = MemoryControlPlaneStore()
    for dep_id, ring in [("dep_int", "internal"), ("dep_pilot", "pilot")]:
        store.create_account_dep = None  # noqa - readability only
        store.create_deployment(CustomerDeployment(id=dep_id, customer_name=dep_id, account_id="acct",
                                                    release_ring=ring, current_version="2026.07.0"))
        store.upsert_module(DeploymentModule(dep_id, "onebrain-api", "0.7.0"))
    store.create_release(ReleaseManifest(version="2026.07.1", git_sha="sha", modules={"onebrain-api": "0.8.0"}))
    return store


def _fake_dispatcher(store):
    """A dispatch_child that creates a child rollout (no real workflow) and records ids."""
    made = []

    def dispatch(fleet_run, deployment_id):
        rid = f"child_{deployment_id}_{len(made)}"
        store.start_rollout(RolloutRun(id=rid, deployment_id=deployment_id,
                                       target_version=fleet_run.target_version, status="pending",
                                       started_by="fleet", fleet_rollout_id=fleet_run.id))
        made.append(rid)
    return dispatch, made


def _succeed(store, dispatch, rid):
    apply_rollout_callback(store, rid, RolloutCallback(status="succeeded"))
    advance_fleet_on_child(store, store, store.get_rollout(rid), dispatch_child=dispatch)


def test_runner_progresses_ring_by_ring_to_succeeded():
    store = _fleet_control()
    dispatch, made = _fake_dispatcher(store)
    fleet_run, plan = plan_and_start_fleet_rollout(
        store, store, fleet_id="f1", target_version="2026.07.1", git_sha="sha",
        failure_tolerance=0, started_by="op", created_at="t", callback_url="https://mc/{rollout_id}",
        dry_run=False, dispatch_child=dispatch)

    assert fleet_run.status == "running" and fleet_run.current_ring == "internal"
    assert made == ["child_dep_int_0"]  # only the internal ring dispatched

    _succeed(store, dispatch, "child_dep_int_0")           # internal ring done -> open pilot
    fr = store.get_fleet_rollout("f1")
    assert fr.current_ring == "pilot" and fr.status == "running"
    assert made[-1].startswith("child_dep_pilot")          # pilot child dispatched

    _succeed(store, dispatch, made[-1])                     # pilot done -> no rings left
    assert store.get_fleet_rollout("f1").status == "succeeded"


def test_runner_pauses_when_ring_failures_exceed_tolerance():
    store = _fleet_control()
    # Two deployments in the internal ring so one can fail.
    store.create_deployment(CustomerDeployment(id="dep_int2", customer_name="dep_int2", account_id="acct",
                                               release_ring="internal", current_version="2026.07.0"))
    store.upsert_module(DeploymentModule("dep_int2", "onebrain-api", "0.7.0"))
    dispatch, made = _fake_dispatcher(store)
    plan_and_start_fleet_rollout(
        store, store, fleet_id="f1", target_version="2026.07.1", git_sha="sha", failure_tolerance=0,
        started_by="op", created_at="t", callback_url="https://mc/{rollout_id}", dry_run=False,
        dispatch_child=dispatch)
    internal_children = list(made)
    assert len(internal_children) == 2

    # One succeeds, one fails -> 1 failure > tolerance 0 -> PAUSE, pilot never opens.
    apply_rollout_callback(store, internal_children[0], RolloutCallback(status="succeeded"))
    advance_fleet_on_child(store, store, store.get_rollout(internal_children[0]), dispatch_child=dispatch)
    apply_rollout_callback(store, internal_children[1], RolloutCallback(status="failed", failure_reason="x"))
    advance_fleet_on_child(store, store, store.get_rollout(internal_children[1]), dispatch_child=dispatch)

    fr = store.get_fleet_rollout("f1")
    assert fr.status == "paused" and fr.current_ring == "internal"
    assert made == internal_children  # no pilot dispatch


# --- fleet store CRUD --------------------------------------------------------

def test_fleet_rollout_store_crud():
    store = MemoryControlPlaneStore()
    fr = store.create_fleet_rollout(FleetRolloutRun(id="f1", target_version="v", status="pending",
                                                    ring_order=("internal",), callback_url="cb", dry_run=True))
    assert store.get_fleet_rollout("f1").target_version == "v"
    assert [f.id for f in store.list_fleet_rollouts()] == ["f1"]
    updated = store.update_fleet_rollout("f1", status="running", current_ring="internal")
    assert updated.status == "running" and updated.current_ring == "internal"
    assert updated.callback_url == "cb"  # preserved
    with pytest.raises(ValueError, match="cannot update fleet rollout"):
        store.update_fleet_rollout("f1", target_version="nope")


# --- operator endpoints ------------------------------------------------------

def _principal(role_id="admin"):
    role = ROLES[role_id]
    return Principal(user_id="op@onebrain", role_id=role.id, role_label=role.label,
                     clearance=role.clearance, locations=None, categories=role.categories, location_label="all")


def _op_settings():
    return SimpleNamespace(operator_mode=True, is_operator_surface=True,
                           provisioning_callback_allowed_hosts="", provisioning_callback_key_id="")


def test_create_fleet_rollout_endpoint(monkeypatch):
    store = _fleet_control()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _op_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: None)
    # Replace the real per-child dispatch with a fake that creates a pending child
    # (as the real one does before it could fail), so reconcile sees it in-flight.
    calls = []

    def fake_child(fid, did, **kw):
        calls.append((fid, did))
        store.start_rollout(RolloutRun(id=f"c_{did}", deployment_id=did, target_version="2026.07.1",
                                       status="pending", started_by="fleet", fleet_rollout_id=fid))
    monkeypatch.setattr(operator_router, "_dispatch_child_rollout", fake_child)
    import app.routers.provisioning as prov
    monkeypatch.setattr(prov, "get_settings", _op_settings)

    out = operator_router.create_fleet_rollout(
        operator_router.FleetRolloutCreate(target_version="2026.07.1",
                                           callback_url="https://mc/{rollout_id}", dry_run=True),
        principal=_principal("admin"))

    assert out.fleet_rollout is not None
    assert out.fleet_rollout.current_ring == "internal"
    assert out.plan.waves == {"internal": ["dep_int"], "pilot": ["dep_pilot"]}
    assert len(calls) == 1 and calls[0][1] == "dep_int"  # only internal ring dispatched


def test_fleet_rollout_pause_resume_abort(monkeypatch):
    store = MemoryControlPlaneStore()
    store.create_fleet_rollout(FleetRolloutRun(id="f1", target_version="v", status="running",
                                               ring_order=("internal",), current_ring="internal"))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _op_settings)
    admin = _principal("admin")

    assert operator_router.pause_fleet_rollout("f1", principal=admin).status == "paused"
    with pytest.raises(HTTPException):  # cannot pause a paused one
        operator_router.pause_fleet_rollout("f1", principal=admin)
    assert operator_router.resume_fleet_rollout("f1", principal=admin).status in {"running", "succeeded"}
    operator_router.update_fleet_rollout = None  # noqa - guard against accidental use
    # abort from running
    store.update_fleet_rollout("f1", status="running")
    assert operator_router.abort_fleet_rollout("f1", principal=admin).status == "aborted"


def test_fleet_rollouts_require_operator_mode(monkeypatch):
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: SimpleNamespace(operator_mode=False, is_operator_surface=True))
    with pytest.raises(HTTPException) as ei:
        operator_router.list_fleet_rollouts(principal=_principal("admin"))
    assert ei.value.status_code == 403


# --- stall + concurrency fixes -----------------------------------------------

def _failing_dispatcher(store):
    """dispatch_child that creates a child then marks it dispatch_failed synchronously
    (no callback will ever arrive) — the stall scenario."""
    from app.controlplane.rollout_exec import mark_rollout_dispatch_failed
    made = []

    def dispatch(fleet_run, deployment_id):
        rid = f"child_{deployment_id}_{len(made)}"
        store.start_rollout(RolloutRun(id=rid, deployment_id=deployment_id,
                                       target_version=fleet_run.target_version, status="pending",
                                       started_by="fleet", fleet_rollout_id=fleet_run.id))
        mark_rollout_dispatch_failed(store, store.get_rollout(rid), "no railway target")
        made.append(rid)
    return dispatch, made


def test_runner_all_sync_dispatch_failures_pause_not_stuck():
    store = _fleet_control()
    dispatch, made = _failing_dispatcher(store)
    plan_and_start_fleet_rollout(
        store, store, fleet_id="f1", target_version="2026.07.1", git_sha="sha", failure_tolerance=0,
        started_by="op", created_at="t", callback_url="cb/{rollout_id}", dry_run=False, dispatch_child=dispatch)
    fr = store.get_fleet_rollout("f1")
    assert fr.status == "paused"                # NOT stuck at 'running'
    assert made == ["child_dep_int_0"]          # pilot ring never opened


def test_runner_advances_through_sync_failures_within_tolerance():
    store = _fleet_control()
    dispatch, _ = _failing_dispatcher(store)
    plan_and_start_fleet_rollout(
        store, store, fleet_id="f2", target_version="2026.07.1", git_sha="sha", failure_tolerance=1,
        started_by="op", created_at="t", callback_url="cb", dry_run=False, dispatch_child=dispatch)
    # each ring has 1 failure <= tolerance 1, so the loop advances through both rings.
    assert store.get_fleet_rollout("f2").status == "succeeded"


def test_ring_redispatch_respects_policy_change():
    """WP4: a deployment whose policy left 'auto' after the fleet rollout
    started must not be re-swept when its ring is dispatched."""
    store = _fleet_control()
    assert _deployments_in_ring(store, "pilot", "2026.07.1") == ["dep_pilot"]

    store.set_update_policy("dep_pilot", "manual")

    assert _deployments_in_ring(store, "pilot", "2026.07.1") == []
    # The other ring is untouched.
    assert _deployments_in_ring(store, "internal", "2026.07.1") == ["dep_int"]


def test_advance_fleet_ring_cas_single_winner():
    store = MemoryControlPlaneStore()
    store.create_fleet_rollout(FleetRolloutRun(id="f", target_version="v", status="running",
                                               ring_order=("internal", "pilot"), current_ring="internal"))
    assert store.advance_fleet_ring("f", "internal", "pilot") is True   # first wins
    assert store.get_fleet_rollout("f").current_ring == "pilot"
    assert store.advance_fleet_ring("f", "internal", "pilot") is False  # from_ring no longer matches
    store.update_fleet_rollout("f", status="paused")
    assert store.advance_fleet_ring("f", "pilot", "early") is False     # not running


# --- P4-07 targeting: named-set / manual-pinned override ----------------------

def test_named_set_restricts_to_listed():
    deps = [_dep("a", "internal"), _dep("b", "internal"), _dep("c", "pilot")]
    plan = plan_fleet_rollout(deps, "v2", _plan_for({}), only_deployment_ids=frozenset({"a", "c"}))
    # b is auto+eligible but never bucketed — it is not in the named set.
    assert {w.ring: list(w.deployment_ids) for w in plan.waves} == {"internal": ["a"], "pilot": ["c"]}
    assert plan.blocked == {} and plan.skipped == ()


def test_named_set_overrides_manual_pinned_policy():
    # A pinned-policy deployment in a real (auto-swept) ring. The PURE planner's fake
    # plan_for returns allowed, so this isolates the policy-override bucketing.
    pinned = SimpleNamespace(id="p", release_ring="pilot", update_policy="pinned")
    plan = plan_fleet_rollout([pinned], "v2", _plan_for({}),
                              only_deployment_ids=frozenset({"p"}), include_manual_pinned=True)
    assert {w.ring: list(w.deployment_ids) for w in plan.waves} == {"pilot": ["p"]}
    # Named but WITHOUT the override flag -> the policy exclusion stands.
    plan2 = plan_fleet_rollout([pinned], "v2", _plan_for({}), only_deployment_ids=frozenset({"p"}))
    assert plan2.waves == ()


def test_manual_pinned_override_redispatched_in_non_first_ring():
    """A16: a NAMED manual deployment in a non-first ring is re-dispatched when its ring
    opens (its plan gate still applies at dispatch — manual passes it). With the
    defaults it is filtered out — pinning the fix and the safe-degradation default."""
    store = _fleet_control()               # dep_int (internal), dep_pilot (pilot)
    store.set_update_policy("dep_pilot", "manual")   # opts out of unconditional auto sweeps

    assert _deployments_in_ring(store, "pilot", "2026.07.1",
                                only_deployment_ids=frozenset({"dep_pilot"}),
                                include_manual_pinned=True) == ["dep_pilot"]
    # Safe-degradation default (e.g. after a mid-rollout MC restart): auto-only filter.
    assert _deployments_in_ring(store, "pilot", "2026.07.1") == []


# --- P4-07 targeting: intra-ring staggering (batch cap) -----------------------

def _ring_of(n: int, ring: str = "internal") -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    for i in range(n):
        store.create_deployment(CustomerDeployment(id=f"d{i}", customer_name=f"d{i}", account_id="acct",
                                                   release_ring=ring, current_version="2026.07.0"))
        store.upsert_module(DeploymentModule(f"d{i}", "onebrain-api", "0.7.0"))
    store.create_release(ReleaseManifest(version="2026.07.1", git_sha="sha", modules={"onebrain-api": "0.8.0"}))
    return store


def test_ring_batch_caps_inflight():
    store = _ring_of(5)
    dispatch, made = _fake_dispatcher(store)
    plan_and_start_fleet_rollout(
        store, store, fleet_id="fb", target_version="2026.07.1", git_sha="sha", failure_tolerance=0,
        started_by="op", created_at="t", callback_url="https://mc/{rollout_id}", dry_run=False,
        dispatch_child=dispatch, ring_batch_size=2)
    assert len(made) == 2   # batch cap: only 2 of 5 in flight

    def _drain(batch):
        for rid in [r for r in made if store.get_rollout(r).status == "pending"]:
            apply_rollout_callback(store, rid, RolloutCallback(status="succeeded"))
        return reconcile_fleet_rollout(store, store, "fb", dispatch_child=dispatch, ring_batch_size=batch)

    _drain(2)
    assert len(made) == 4   # next 2 opened only after the first batch drained
    _drain(2)
    assert len(made) == 5   # the last 1
    _drain(2)
    assert store.get_fleet_rollout("fb").status == "succeeded"   # ring advances only when all 5 drained


def test_ring_batch_zero_dispatches_whole_ring():
    store = _ring_of(4)
    dispatch, made = _fake_dispatcher(store)
    plan_and_start_fleet_rollout(
        store, store, fleet_id="fz", target_version="2026.07.1", git_sha="sha", failure_tolerance=0,
        started_by="op", created_at="t", callback_url="https://mc/{rollout_id}", dry_run=False,
        dispatch_child=dispatch, ring_batch_size=0)
    assert len(made) == 4   # batch_size=0 -> whole ring at once (today's behavior)


def test_batch_failure_still_pauses_via_reducer():
    store = _ring_of(3)
    dispatch, made = _fake_dispatcher(store)
    plan_and_start_fleet_rollout(
        store, store, fleet_id="fb", target_version="2026.07.1", git_sha="sha", failure_tolerance=0,
        started_by="op", created_at="t", callback_url="cb/{rollout_id}", dry_run=False,
        dispatch_child=dispatch, ring_batch_size=2)
    assert len(made) == 2

    # One of the first batch fails -> the reducer pauses at drain (1 failure > tolerance 0),
    # and the next batch never opens (a failure is never re-dispatched by the batcher).
    apply_rollout_callback(store, made[0], RolloutCallback(status="succeeded"))
    apply_rollout_callback(store, made[1], RolloutCallback(status="failed", failure_reason="boom"))
    reconcile_fleet_rollout(store, store, "fb", dispatch_child=dispatch, ring_batch_size=2)

    assert store.get_fleet_rollout("fb").status == "paused"
    assert len(made) == 2


def test_create_fleet_rollout_threads_targeting(monkeypatch):
    store = _fleet_control()   # dep_int (internal), dep_pilot (pilot)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _op_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: None)
    calls = []

    def fake_child(fid, did, **kw):
        calls.append(did)
        store.start_rollout(RolloutRun(id=f"c_{did}", deployment_id=did, target_version="2026.07.1",
                                       status="pending", started_by="fleet", fleet_rollout_id=fid))
    monkeypatch.setattr(operator_router, "_dispatch_child_rollout", fake_child)
    import app.routers.provisioning as prov
    monkeypatch.setattr(prov, "get_settings", _op_settings)

    out = operator_router.create_fleet_rollout(
        operator_router.FleetRolloutCreate(target_version="2026.07.1", callback_url="https://mc/{rollout_id}",
                                           dry_run=True, deployment_ids=["dep_pilot"]),
        principal=_principal("admin"))
    # Named set {dep_pilot}: dep_int (internal) is never bucketed; only pilot runs.
    assert out.plan.waves == {"pilot": ["dep_pilot"]}
    assert calls == ["dep_pilot"]

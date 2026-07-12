"""P4-06: pull-path reconcile — the pure synthesis + the MC-side tick that turns a
box's fleet.v2 UpdateReport into a child-rollout terminal status and feeds the
UNCHANGED fleet reducer (reconcile_fleet_rollout -> advance_fleet_rollout). Railway-
free: control/fleet stores, the latest-heartbeats snapshot, and the clock are injected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest, RolloutRun
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.orchestration import FleetRolloutRun
from app.controlplane.pull_reconcile import (
    materialize_backup_from_report,
    reconcile_pull_targets,
    synthesize_pull_status,
)
from app.fleet.heartbeat import UpdateReport, build_heartbeat_v2

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
DEADLINE = 1800
# A well-formed backup manifest (7d/A17): sha256:<64hex>:<bytes> of the encrypted backup.
_MANIFEST = "sha256:" + "a" * 64 + ":4096"


# --- pure synthesis ----------------------------------------------------------

def _child(child_id: str = "c1", *, dispatched_at: str = NOW.isoformat()):
    return SimpleNamespace(id=child_id, dispatched_at=dispatched_at)


def _match(outcome: str) -> UpdateReport:
    return UpdateReport(attempt_id="c1", outcome=outcome)


def test_synthesize_matrix():
    fresh = _child(dispatched_at=NOW.isoformat())                                   # 0s ago
    stale = _child(dispatched_at=(NOW - timedelta(seconds=DEADLINE + 60)).isoformat())  # past deadline

    # attempt_id != child.id (silent-for-THIS-offer box): before deadline wait, past fail.
    other = UpdateReport(attempt_id="other", outcome="succeeded")
    assert synthesize_pull_status(fresh, other, now=NOW, deadline_seconds=DEADLINE) is None
    assert synthesize_pull_status(stale, other, now=NOW, deadline_seconds=DEADLINE) == "failed"

    # attempt matches:
    assert synthesize_pull_status(fresh, _match("succeeded"), now=NOW, deadline_seconds=DEADLINE) == "success"
    assert synthesize_pull_status(fresh, _match("failed"), now=NOW, deadline_seconds=DEADLINE) == "failed"
    assert synthesize_pull_status(fresh, _match("rolled_back"), now=NOW, deadline_seconds=DEADLINE) == "failed"
    # in_progress / none: before deadline keep waiting, past deadline timeout-fail.
    assert synthesize_pull_status(fresh, _match("in_progress"), now=NOW, deadline_seconds=DEADLINE) is None
    assert synthesize_pull_status(stale, _match("in_progress"), now=NOW, deadline_seconds=DEADLINE) == "failed"
    assert synthesize_pull_status(fresh, _match("none"), now=NOW, deadline_seconds=DEADLINE) is None
    # garbled / missing dispatched_at -> 'no deadline yet' -> None (never immediate fail).
    assert synthesize_pull_status(_child(dispatched_at="not-a-date"), _match("in_progress"),
                                  now=NOW, deadline_seconds=DEADLINE) is None
    assert synthesize_pull_status(_child(dispatched_at=""), other, now=NOW, deadline_seconds=DEADLINE) is None


# --- the tick ----------------------------------------------------------------

def _hb(deployment_id: str, *, attempt_id: str = "", outcome: str = "none",
        backup_status: str = "", backup_ts: str = "", backup_manifest: str = ""):
    """A stored-heartbeat stand-in whose .payload matches latest_heartbeats()'s shape."""
    body = build_heartbeat_v2(
        deployment_id=deployment_id, reported_at=NOW.isoformat(), version="2026.07.1",
        update=UpdateReport(attempt_id=attempt_id, outcome=outcome, last_target_version="2026.07.1",
                            backup_status=backup_status, backup_ts=backup_ts,
                            backup_manifest=backup_manifest))
    return SimpleNamespace(payload=body.model_dump())


def _store_with_offered_child(*, migration_to: str = "0041", current_migration: str = "0041", tol: int = 0):
    """A running fleet rollout with ONE offered pull child — exactly what
    offer_pull_target produces: started, claimed, marked dispatched with a
    dispatched_at anchor and the pull request_payload."""
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id="dep_p", customer_name="dep_p", account_id="acct", release_ring="pilot",
        current_version="2026.07.0", current_migration=current_migration))
    store.upsert_module(DeploymentModule("dep_p", "onebrain-api", "0.7.0"))
    store.create_release(ReleaseManifest(
        version="2026.07.1", git_sha="sha", modules={"onebrain-api": "0.8.0"},
        migration_from="0041", migration_to=migration_to))
    store.create_fleet_rollout(FleetRolloutRun(
        id="f1", target_version="2026.07.1", status="running", ring_order=("pilot",),
        current_ring="pilot", failure_tolerance=tol, callback_url="https://mc/{rollout_id}", dry_run=False))
    store.start_rollout(RolloutRun(id="c_dep", deployment_id="dep_p", target_version="2026.07.1",
                                   status="pending", started_by="fleet:f1", fleet_rollout_id="f1"))
    store.claim_rollout_dispatch("c_dep")
    store.update_rollout_exec("c_dep", dispatched_at=NOW.isoformat(),
                              request_payload={"provider": "hetzner", "pull": True})
    return store


def _noop_dispatch(fleet_run, deployment_id):
    return None


def test_reconcile_advances_parent_on_success():
    store = _store_with_offered_child()
    heartbeats = {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded")}
    runs = reconcile_pull_targets(store, store, heartbeats, now=NOW, deadline_seconds=DEADLINE,
                                  dispatch_child=_noop_dispatch)

    child = store.get_rollout("c_dep")
    assert child.status == "success" and child.exec_status == "succeeded"
    # 'success' went through the UNCHANGED update_rollout_status gate -> release applied.
    assert store.get_deployment("dep_p").current_version == "2026.07.1"
    # The parent advanced through the UNCHANGED reducer (single ring -> succeeded).
    assert store.get_fleet_rollout("f1").status == "succeeded"
    assert [r.id for r in runs] == ["f1"]


def test_reconcile_pauses_parent_on_timeout():
    store = _store_with_offered_child(tol=0)
    later = NOW + timedelta(seconds=DEADLINE + 60)   # box silent well past the deadline
    reconcile_pull_targets(store, store, {}, now=later, deadline_seconds=DEADLINE,
                           dispatch_child=_noop_dispatch)

    child = store.get_rollout("c_dep")
    assert child.status == "failed" and child.exec_status == "failed"
    assert child.failure_reason == "pull_convergence_timeout"
    assert store.get_fleet_rollout("f1").status == "paused"   # 1 failure > tolerance 0


def test_reconcile_still_running_before_deadline():
    store = _store_with_offered_child()
    # Box reports in_progress for THIS offer, and now is before the deadline.
    heartbeats = {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="in_progress")}
    reconcile_pull_targets(store, store, heartbeats, now=NOW, deadline_seconds=DEADLINE,
                           dispatch_child=_noop_dispatch)
    assert store.get_rollout("c_dep").status == "pending"           # untouched, still waiting
    assert store.get_fleet_rollout("f1").status == "running"


def test_reconcile_materializes_backup():
    store = _store_with_offered_child()   # non-crossing child (offerable without a prior backup)
    # A migration-crossing release a plan to which is backup-blocked before any backup.
    store.create_release(ReleaseManifest(version="2026.08.0", git_sha="sha2",
                                         modules={"onebrain-api": "0.9.0"},
                                         migration_from="0041", migration_to="0050"))
    assert store.plan_update("dep_p", "2026.08.0").reason == "backup_required_for_schema_update"

    heartbeats = {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded",
                               backup_status="success", backup_ts="2026-07-12T11:59:00+00:00",
                               backup_manifest=_MANIFEST)}
    reconcile_pull_targets(store, store, heartbeats, now=NOW, deadline_seconds=DEADLINE,
                           dispatch_child=_noop_dispatch)

    assert store.latest_backup("dep_p").status == "success"
    # The self-reported backup now satisfies the plan gate for the crossing release.
    assert store.plan_update("dep_p", "2026.08.0").reason != "backup_required_for_schema_update"


def test_materialize_backup_is_idempotent_on_same_ts():
    store = _store_with_offered_child()
    report = UpdateReport(backup_status="success", backup_ts="2026-07-12T11:59:00+00:00",
                          backup_manifest=_MANIFEST)
    materialize_backup_from_report(store, "dep_p", report)
    materialize_backup_from_report(store, "dep_p", report)   # re-tick of the same heartbeat
    # No duplicate row / no raise; the single backup stands. The manifest lands in detail
    # so the operator can cross-check it (7d).
    assert store.latest_backup("dep_p").detail == f"pull-report:2026-07-12T11:59:00+00:00:{_MANIFEST}"
    # An absent/failed backup claim records nothing.
    materialize_backup_from_report(store, "dep_p", UpdateReport(backup_status="failed", backup_ts="x"))
    assert store.latest_backup("dep_p").status == "success"


def test_materialize_backup_requires_well_formed_manifest():
    """7d/A17: a bare/garbled 'success' is NOT a backup (a phantom-backup box cannot
    disable its own restore net); only a well-formed sha256:<64hex>:<bytes> materializes."""
    from app.controlplane.pull_reconcile import parse_backup_manifest

    # Naked success (no manifest) -> nothing materialized.
    store = _store_with_offered_child()
    materialize_backup_from_report(
        store, "dep_p", UpdateReport(backup_status="success", backup_ts="2026-07-12T11:59:00+00:00"))
    assert store.latest_backup("dep_p") is None

    # Garbled manifests (wrong algo, short hex, non-numeric size) -> still nothing.
    for bad in ("success", "sha256:" + "a" * 63 + ":1", "sha256:" + "a" * 64 + ":x",
                "md5:" + "a" * 64 + ":1", "sha256:" + "A" * 64 + ":1"):
        assert parse_backup_manifest(bad) is None
        materialize_backup_from_report(
            store, "dep_p",
            UpdateReport(backup_status="success", backup_ts="2026-07-12T11:59:00+00:00",
                         backup_manifest=bad))
        assert store.latest_backup("dep_p") is None

    # A well-formed manifest materializes, carrying the manifest in detail.
    assert parse_backup_manifest(_MANIFEST) == _MANIFEST
    materialize_backup_from_report(
        store, "dep_p", UpdateReport(backup_status="success", backup_ts="2026-07-12T11:59:00+00:00",
                                     backup_manifest=_MANIFEST))
    bk = store.latest_backup("dep_p")
    assert bk is not None and bk.status == "success" and _MANIFEST in bk.detail


def test_reconcile_ignores_railway_children():
    store = _store_with_offered_child()
    # Re-flag the child as a RAILWAY child (no pull marker) — its workflow callback owns it.
    store.update_rollout_exec("c_dep", request_payload={"dry_run": True})
    heartbeats = {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded")}
    reconcile_pull_targets(store, store, heartbeats, now=NOW, deadline_seconds=DEADLINE,
                           dispatch_child=_noop_dispatch)

    assert store.get_rollout("c_dep").status == "pending"        # untouched by the pull tick
    assert store.get_fleet_rollout("f1").status == "running"


def test_reconcile_at_rest_is_noop():
    store = MemoryControlPlaneStore()   # no fleet rollouts
    assert reconcile_pull_targets(store, store, {}, now=NOW, deadline_seconds=DEADLINE,
                                  dispatch_child=_noop_dispatch) == []


def test_reconcile_endpoint_drives_the_tick(monkeypatch):
    import app.routers.operator as operator_router
    from app.auth.principal import Principal
    from app.auth.roles import ROLES

    store = _store_with_offered_child()

    class _FleetStore:
        def latest_heartbeats(self):
            return {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded")}

    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_fleet_store", lambda: _FleetStore())
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: SimpleNamespace(operator_mode=True, is_operator_surface=True,
                                                fleet_pull_convergence_deadline_seconds=DEADLINE))
    role = ROLES["admin"]
    principal = Principal(user_id="op@onebrain", role_id=role.id, role_label=role.label,
                          clearance=role.clearance, locations=None, categories=role.categories,
                          location_label="all")

    out = operator_router.reconcile_pull(principal=principal)
    assert [r.status for r in out] == ["succeeded"]     # the offered pull child drove f1 to done
    assert store.get_fleet_rollout("f1").status == "succeeded"

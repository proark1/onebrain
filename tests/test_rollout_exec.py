"""Rollout state and Hetzner pull-target behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.operator as operator_router
import app.routers.provisioning as provisioning_router
import app.routers.rollouts as rollouts_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest, RolloutRun
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.rollout_exec import (
    resolve_provisioned_target,
    target_provider,
)


def _principal(role_id: str = "admin", user_id: str = "op@onebrain") -> Principal:
    role = ROLES[role_id]
    p = Principal(user_id=user_id, role_id=role.id, role_label=role.label, clearance=role.clearance,
                  locations=None, categories=role.categories, location_label="all")
    return p


def _control(schema_change: bool = False, backed_up: bool = False) -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id="dep_a", customer_name="A", account_id="acct_a", release_ring="pilot",
        current_version="2026.07.0", current_migration="0041"))
    store.upsert_module(DeploymentModule("dep_a", "onebrain-api", "0.7.0"))
    store.create_release(ReleaseManifest(
        version="2026.07.1", git_sha="abc123", modules={"onebrain-api": "0.8.0"},
        migration_from="0041", migration_to="0042" if schema_change else "0041"))
    if backed_up:
        from app.controlplane.base import BackupRun
        store.record_backup(BackupRun("bak", "dep_a", "success", "ready"))
    return store


def _started(store) -> RolloutRun:
    return store.start_rollout(RolloutRun(
        id="roll1", deployment_id="dep_a", target_version="2026.07.1", status="pending", started_by="op"))


def test_legacy_rollout_callback_route_is_not_exposed():
    assert rollouts_router.router.routes == []


# --- Hetzner target resolver -------------------------------------------------

class _FakeProvStore:
    def __init__(self, runs):
        self._runs = runs

    def list_runs(self, account_id="", deployment_id=""):
        return [r for r in self._runs if not deployment_id or r.deployment_id == deployment_id]


def test_resolve_provisioned_target_picks_latest_succeeded():
    runs = [
        SimpleNamespace(id="r1", deployment_id="dep_a", status="succeeded", railway_project_id="p_old",
                        railway_environment_id="e", result_payload={"service_ids": {"a": "1"}},
                        completed_at="2026-07-11T00:00:00", created_at="t"),
        SimpleNamespace(id="r2", deployment_id="dep_a", status="succeeded", railway_project_id="p_new",
                        railway_environment_id="e2", result_payload={"service_ids": {"a": "2"}},
                        completed_at="2026-07-11T01:00:00", created_at="t"),
        SimpleNamespace(id="r3", deployment_id="dep_a", status="failed", railway_project_id="p_bad",
                        railway_environment_id="", result_payload={}, completed_at="t", created_at="t"),
    ]
    target = resolve_provisioned_target(_FakeProvStore(runs), "dep_a")
    assert target["target_id"] == "p_new" and target["service_ids"] == {"a": "2"}


def test_resolve_provisioned_target_fail_closed_when_none():
    with pytest.raises(ValueError, match="no successful Hetzner"):
        resolve_provisioned_target(_FakeProvStore([]), "dep_a")


# --- dedicated_server target semantics (WP5, D-6) -----------------------------

def _hetzner_prov_run():
    """A provisioning run using the D-6 slot convention: the Hetzner provisioner
    (P1) writes Hetzner coordinates into the EXISTING Railway-named columns."""
    return SimpleNamespace(
        id="r1", deployment_id="dep_a", status="succeeded",
        railway_project_id="hetzner:2481632", railway_environment_id="onebrain-dep_a",
        result_payload={"service_ids": {"onebrain-api": "onebrain-api"}},
        completed_at="t", created_at="t")


def test_resolve_provisioned_target_resolves_hetzner_style_run():
    # The fail-closed resolver is unchanged — hetzner-slotted coordinates
    # resolve like any succeeded run; classification is target_provider's job.
    target = resolve_provisioned_target(_FakeProvStore([_hetzner_prov_run()]), "dep_a")

    assert target["target_id"] == "hetzner:2481632"
    assert target["target_environment"] == "onebrain-dep_a"
    assert target["service_ids"] == {"onebrain-api": "onebrain-api"}
    assert target_provider(target) == "hetzner"


def test_target_provider_rejects_non_hetzner_targets():
    assert target_provider({"target_id": "unknown", "target_environment": "env"}) == "unknown"
    assert target_provider({}) == "unknown"  # resolver already fail-closes empties upstream


# --- store methods -----------------------------------------------------------

def test_store_list_active_rollout_and_update_exec():
    store = _control()
    assert store.list_active_rollout("dep_a") is None
    _started(store)
    assert store.list_active_rollout("dep_a").id == "roll1"

    store.update_rollout_exec("roll1", exec_status="dispatched", external_run_url="wf")
    assert store.get_rollout("roll1").exec_status == "dispatched"
    with pytest.raises(ValueError, match="cannot update rollout exec"):
        store.update_rollout_exec("roll1", status="success")  # not an exec field

    store.update_rollout_status("roll1", "success")
    store.update_rollout_exec("roll1", exec_status="succeeded", completed_at="now")
    assert store.list_active_rollout("dep_a") is None  # terminal, no longer active


# --- dispatch endpoint (guards) ----------------------------------------------

def _operator_settings(**over):
    data = dict(operator_mode=True, is_operator_surface=True, provisioning_callback_key_id="",
                provisioning_callback_allowed_hosts="")
    data.update(over)
    return SimpleNamespace(**data)


def _dispatch_body():
    return operator_router.RolloutDispatch(
        callback_url="https://mc.example/api/rollouts/{rollout_id}/callback", dry_run=True)


def test_deployment_create_defaults_to_supported_hetzner_type():
    assert operator_router.DeploymentCreate(customer_name="A").deployment_type == "dedicated_server"
    for retired in ("dedicated_railway", "shared_railway"):
        with pytest.raises(ValueError):
            operator_router.DeploymentCreate(customer_name="A", deployment_type=retired)


def test_dispatch_rollout_rejects_missing_and_active_and_blocked(monkeypatch):
    store = _control()
    rollout = _started(store)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(provisioning_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: _FakeProvStore([]))
    admin = _principal("admin")

    with pytest.raises(HTTPException) as ei:  # unknown rollout
        operator_router.dispatch_rollout("dep_a", "ghost", _dispatch_body(), principal=admin)
    assert ei.value.status_code == 404

    # No provisioned target -> dispatch_failed + 409, and the rollout is terminal-failed.
    with pytest.raises(HTTPException) as ei:
        operator_router.dispatch_rollout("dep_a", "roll1", _dispatch_body(), principal=admin)
    assert ei.value.status_code == 409
    assert store.get_rollout("roll1").exec_status == "dispatch_failed"

    # Already-dispatched (exec_status not pending) is rejected.
    store2 = _control()
    _started(store2)
    store2.update_rollout_exec("roll1", exec_status="dispatched")
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store2)
    with pytest.raises(HTTPException) as ei:
        operator_router.dispatch_rollout("dep_a", "roll1", _dispatch_body(), principal=admin)
    assert ei.value.status_code == 409


def test_dispatch_rollout_rejects_non_hetzner_target(monkeypatch):
    store = _control()
    _started(store)
    prov = _FakeProvStore([SimpleNamespace(
        id="r1", deployment_id="dep_a", status="succeeded", railway_project_id="p1",
        railway_environment_id="e1", result_payload={"service_ids": {}}, completed_at="t", created_at="t")])
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(provisioning_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: prov)

    with pytest.raises(HTTPException) as exc:
        operator_router.dispatch_rollout("dep_a", "roll1", _dispatch_body(), principal=_principal("admin"))

    assert exc.value.status_code == 409
    assert "not a Hetzner" in exc.value.detail
    assert store.get_rollout("roll1").exec_status == "dispatch_failed"


def test_dispatch_offers_hetzner_target(monkeypatch):
    """A Hetzner box receives the rollout through its signed desired state."""
    store = _control()
    _started(store)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(provisioning_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store",
                        lambda: _FakeProvStore([_hetzner_prov_run()]))

    out = operator_router.dispatch_rollout("dep_a", "roll1", _dispatch_body(), principal=_principal("admin"))

    assert out.id == "roll1"  # 200 — RolloutOut returned, no HTTPException
    rollout = store.get_rollout("roll1")
    assert rollout.exec_status == "dispatched" and rollout.dispatched_at
    assert rollout.request_payload == {"provider": "hetzner", "pull": True}


def test_fleet_child_offers_hetzner_target(monkeypatch):
    """The fleet child remains in-flight until the box reports its outcome."""
    store = _control()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store",
                        lambda: _FakeProvStore([_hetzner_prov_run()]))

    operator_router._dispatch_child_rollout(
        "fleet_x", "dep_a", target_version="2026.07.1",
        callback_url="https://mc/api/rollouts/{rollout_id}/callback", dry_run=True)

    children = store.list_rollouts_for_fleet("fleet_x")
    assert len(children) == 1
    assert children[0].status == "pending"          # non-terminal, awaiting box convergence
    assert children[0].exec_status == "dispatched"  # offered, not dispatch_failed
    assert children[0].request_payload == {"provider": "hetzner", "pull": True}


def test_fleet_child_rejects_non_hetzner_target(monkeypatch):
    store = _control()
    non_hetzner = SimpleNamespace(
        id="r1", deployment_id="dep_a", status="succeeded", railway_project_id="p1",
        railway_environment_id="e1", result_payload={"service_ids": {}}, completed_at="t", created_at="t")
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store",
                        lambda: _FakeProvStore([non_hetzner]))

    operator_router._dispatch_child_rollout(
        "fleet_x", "dep_a", target_version="2026.07.1",
        callback_url="https://mc/api/rollouts/{rollout_id}/callback", dry_run=True)

    child = store.list_rollouts_for_fleet("fleet_x")[0]
    assert child.exec_status == "dispatch_failed"
    assert child.failure_reason == "Rollout target is not a Hetzner deployment."


def test_fleet_child_dispatch_vanished_row_never_raises(monkeypatch):
    """If the child row vanishes between start_rollout and the read-back, the
    dispatch path returns quietly: nothing to mark failed, nothing dispatched,
    and the never-raises contract holds (the None-guard runs before any
    dereference of the read-back rollout)."""
    store = _control()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(store, "get_rollout", lambda rollout_id: None)

    operator_router._dispatch_child_rollout(
        "fleet_x", "dep_a", target_version="2026.07.1",
        callback_url="https://mc/api/rollouts/{rollout_id}/callback", dry_run=True)

    children = store.list_rollouts_for_fleet("fleet_x")
    assert len(children) == 1
    assert children[0].status == "pending"       # created, never marked failed
    assert children[0].exec_status == "pending"  # no dispatch_failed bookkeeping


def test_claim_rollout_dispatch_is_single_winner():
    store = _control()
    _started(store)
    assert store.claim_rollout_dispatch("roll1") is True          # first wins
    assert store.get_rollout("roll1").exec_status == "dispatched"
    assert store.claim_rollout_dispatch("roll1") is False         # second loses (not pending)
    assert store.claim_rollout_dispatch("nope") is False          # unknown id

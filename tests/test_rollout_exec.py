"""Fleet rollout executor: dispatch-input builder, callback state machine,
Railway-target resolver, dispatcher, store methods, and the operator/callback
routers. All Railway-free — the dry_run seam + injected opener keep it unit-level.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.operator as operator_router
import app.routers.rollouts as rollouts_router
import app.routers.provisioning as provisioning_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest, RolloutRun
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.rollout_exec import (
    RolloutCallback,
    apply_rollout_callback,
    build_rollout_dispatch_inputs,
    mark_rollout_dispatch_failed,
    resolve_railway_target,
    target_provider,
)
from app.provisioning.runs import RolloutWorkflowDispatcher, dispatch_workflow


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


# --- dispatch input builder (pure) -------------------------------------------

def test_build_rollout_dispatch_inputs():
    store = _control()
    rollout = _started(store)
    inputs = build_rollout_dispatch_inputs(
        rollout=rollout, plan=store.plan_update("dep_a", "2026.07.1"),
        release=store.get_release("2026.07.1"), deployment=store.get_deployment("dep_a"),
        railway={"railway_project_id": "proj1", "railway_environment_id": "env1", "service_ids": {"onebrain-api": "s1"}},
        callback_url="https://mc/api/rollouts/{rollout_id}/callback", callback_key_id="key1", dry_run=True)

    assert inputs["rollout_id"] == "roll1"
    assert inputs["deployment_id"] == "dep_a" and inputs["account_id"] == "acct_a"
    assert inputs["target_version"] == "2026.07.1" and inputs["git_sha"] == "abc123"
    assert inputs["modules_to_update_json"] == '{"onebrain-api": "0.8.0"}'
    assert inputs["callback_url"] == "https://mc/api/rollouts/roll1/callback"  # {rollout_id} substituted
    assert inputs["dry_run"] == "true"
    assert inputs["railway_project_id"] == "proj1"


# --- callback state machine --------------------------------------------------

def test_apply_rollout_callback_running_then_succeeded_applies_versions():
    store = _control()
    _started(store)

    r = apply_rollout_callback(store, "roll1", RolloutCallback(status="running", external_run_url="u"))
    assert r.exec_status == "running" and r.status == "running" and r.external_run_url == "u"

    r = apply_rollout_callback(store, "roll1", RolloutCallback(status="succeeded", smoke_status="ok"))
    assert r.exec_status == "succeeded" and r.status == "success" and r.completed_at
    # The bookkeeping success path applied the release's module + version.
    assert store.get_deployment("dep_a").current_version == "2026.07.1"
    assert {m.module_id: m.version for m in store.list_modules("dep_a")}["onebrain-api"] == "0.8.0"


def test_apply_rollout_callback_terminal_and_backward_guards():
    store = _control()
    _started(store)
    apply_rollout_callback(store, "roll1", RolloutCallback(status="succeeded"))
    with pytest.raises(ValueError, match="terminal"):
        apply_rollout_callback(store, "roll1", RolloutCallback(status="succeeded"))

    store2 = _control()
    store2.start_rollout(RolloutRun(id="roll2", deployment_id="dep_a", target_version="2026.07.1",
                                    status="pending", started_by="op"))
    apply_rollout_callback(store2, "roll2", RolloutCallback(status="running"))
    with pytest.raises(ValueError, match="backward"):
        apply_rollout_callback(store2, "roll2", RolloutCallback(status="dispatched"))  # rank 2 < running 3


def test_apply_rollout_callback_failed_sets_reason_and_does_not_bump():
    store = _control()
    _started(store)
    r = apply_rollout_callback(store, "roll1", RolloutCallback(status="failed", failure_reason="boom"))
    assert r.exec_status == "failed" and r.status == "failed" and r.failure_reason == "boom"
    assert store.get_deployment("dep_a").current_version == "2026.07.0"  # unchanged


def test_apply_rollout_callback_unknown_id():
    with pytest.raises(KeyError):
        apply_rollout_callback(_control(), "nope", RolloutCallback(status="running"))


def test_apply_rollout_callback_schema_change_needs_backup_at_apply():
    # A schema-changing release with a backup starts fine; if the backup is missing
    # at apply time the succeeded callback is refused (update_rollout_status re-runs
    # plan_update). We assert the with-backup happy path applies the migration.
    store = _control(schema_change=True, backed_up=True)
    _started(store)
    apply_rollout_callback(store, "roll1", RolloutCallback(status="succeeded"))
    assert store.get_deployment("dep_a").current_migration == "0042"


# --- railway target resolver -------------------------------------------------

class _FakeProvStore:
    def __init__(self, runs):
        self._runs = runs

    def list_runs(self, account_id="", deployment_id=""):
        return [r for r in self._runs if not deployment_id or r.deployment_id == deployment_id]


def test_resolve_railway_target_picks_latest_succeeded():
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
    target = resolve_railway_target(_FakeProvStore(runs), "dep_a")
    assert target["railway_project_id"] == "p_new" and target["service_ids"] == {"a": "2"}


def test_resolve_railway_target_fail_closed_when_none():
    with pytest.raises(ValueError, match="no successful provisioning"):
        resolve_railway_target(_FakeProvStore([]), "dep_a")


# --- dedicated_server target semantics (WP5, D-6) -----------------------------

def _hetzner_prov_run():
    """A provisioning run using the D-6 slot convention: the Hetzner provisioner
    (P1) writes Hetzner coordinates into the EXISTING Railway-named columns."""
    return SimpleNamespace(
        id="r1", deployment_id="dep_a", status="succeeded",
        railway_project_id="hetzner:2481632", railway_environment_id="onebrain-dep_a",
        result_payload={"service_ids": {"onebrain-api": "onebrain-api"}},
        completed_at="t", created_at="t")


def test_resolve_railway_target_resolves_hetzner_style_run():
    # The fail-closed resolver is unchanged — hetzner-slotted coordinates
    # resolve like any succeeded run; classification is target_provider's job.
    target = resolve_railway_target(_FakeProvStore([_hetzner_prov_run()]), "dep_a")

    assert target["railway_project_id"] == "hetzner:2481632"
    assert target["railway_environment_id"] == "onebrain-dep_a"
    assert target["service_ids"] == {"onebrain-api": "onebrain-api"}
    assert target_provider(target) == "hetzner"


def test_target_provider_railway_default():
    assert target_provider({"railway_project_id": "proj1", "railway_environment_id": "env1"}) == "railway"
    assert target_provider({}) == "railway"  # resolve already fail-closes empties upstream


# --- dispatcher (injected opener, no network) --------------------------------

class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gh_settings(**over):
    data = dict(github_owner="o", github_repo="repo", github_update_workflow="update-customer.yml",
                github_ref="main", github_dispatch_token="tok")
    data.update(over)
    return SimpleNamespace(**data)


def test_rollout_dispatcher_enabled_and_dispatch():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["auth"] = request.headers.get("Authorization")
        return _FakeResp()

    dispatcher = RolloutWorkflowDispatcher(_gh_settings())
    assert dispatcher.enabled is True
    url = dispatcher.dispatch({"rollout_id": "roll1", "dry_run": "true"}, opener=opener)

    assert "update-customer.yml" in captured["url"]
    assert b'"rollout_id": "roll1"' in captured["body"]
    assert captured["auth"] == "Bearer tok"
    assert "actions/workflows/update-customer.yml" in url


def test_rollout_dispatcher_disabled_when_unconfigured():
    assert RolloutWorkflowDispatcher(_gh_settings(github_dispatch_token="")).enabled is False
    with pytest.raises(RuntimeError, match="not configured"):
        RolloutWorkflowDispatcher(_gh_settings(github_dispatch_token="")).dispatch({})


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

    apply_rollout_callback(store, "roll1", RolloutCallback(status="succeeded"))
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

    # No railway target -> dispatch_failed + 409, and the rollout is terminal-failed.
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


def test_dispatch_rollout_success_path(monkeypatch):
    store = _control()
    _started(store)
    prov = _FakeProvStore([SimpleNamespace(
        id="r1", deployment_id="dep_a", status="succeeded", railway_project_id="p1",
        railway_environment_id="e1", result_payload={"service_ids": {}}, completed_at="t", created_at="t")])
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(provisioning_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: prov)

    class _FakeDispatcher:
        def __init__(self, settings):
            pass

        def dispatch(self, inputs, opener=None):
            return "https://github.com/o/repo/actions/workflows/update-customer.yml"

    monkeypatch.setattr(operator_router, "RolloutWorkflowDispatcher", _FakeDispatcher)

    out = operator_router.dispatch_rollout("dep_a", "roll1", _dispatch_body(), principal=_principal("admin"))
    assert out.status  # RolloutOut returned
    assert store.get_rollout("roll1").exec_status == "dispatched"
    assert store.get_rollout("roll1").external_run_url.endswith("update-customer.yml")


class _RecordingDispatcher:
    """Fake RolloutWorkflowDispatcher that must NEVER fire for a Hetzner target."""
    calls: list = []

    def __init__(self, settings):
        pass

    def dispatch(self, inputs, opener=None):
        _RecordingDispatcher.calls.append(inputs)
        return "https://github.com/o/repo/actions/workflows/update-customer.yml"


def test_dispatch_refuses_hetzner_target(monkeypatch):
    """Fail-closed (D-6): the GitHub/Railway executor cannot act on a Hetzner
    box — the operator dispatch 409s, the rollout terminal-fails unclaimed, and
    the workflow dispatcher is never invoked."""
    store = _control()
    _started(store)
    _RecordingDispatcher.calls = []
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(provisioning_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store",
                        lambda: _FakeProvStore([_hetzner_prov_run()]))
    monkeypatch.setattr(operator_router, "RolloutWorkflowDispatcher", _RecordingDispatcher)

    with pytest.raises(HTTPException) as ei:
        operator_router.dispatch_rollout("dep_a", "roll1", _dispatch_body(), principal=_principal("admin"))

    assert ei.value.status_code == 409
    assert ei.value.detail.startswith("unsupported_dispatch_provider:hetzner")
    rollout = store.get_rollout("roll1")
    assert rollout.exec_status == "dispatch_failed"
    assert rollout.failure_reason.startswith("unsupported_dispatch_provider:hetzner")
    assert _RecordingDispatcher.calls == []  # never fired


def test_fleet_child_dispatch_refuses_hetzner_target(monkeypatch):
    """The fleet child path never raises: the child is marked dispatch_failed
    (bookkeeping 'failed') so the reducer counts it toward failure_tolerance,
    exactly like a missing target today."""
    store = _control()
    _RecordingDispatcher.calls = []
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store",
                        lambda: _FakeProvStore([_hetzner_prov_run()]))
    monkeypatch.setattr(operator_router, "RolloutWorkflowDispatcher", _RecordingDispatcher)

    operator_router._dispatch_child_rollout(
        "fleet_x", "dep_a", target_version="2026.07.1",
        callback_url="https://mc/api/rollouts/{rollout_id}/callback", dry_run=True)

    children = store.list_rollouts_for_fleet("fleet_x")
    assert len(children) == 1
    assert children[0].status == "failed"
    assert children[0].exec_status == "dispatch_failed"
    assert children[0].failure_reason.startswith("unsupported_dispatch_provider:hetzner")
    assert _RecordingDispatcher.calls == []  # never fired


def test_fleet_child_dispatch_vanished_row_never_raises(monkeypatch):
    """If the child row vanishes between start_rollout and the read-back, the
    dispatch path returns quietly: nothing to mark failed, nothing dispatched,
    and the never-raises contract holds (the None-guard runs before any
    dereference of the read-back rollout)."""
    store = _control()
    _RecordingDispatcher.calls = []
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "RolloutWorkflowDispatcher", _RecordingDispatcher)
    monkeypatch.setattr(store, "get_rollout", lambda rollout_id: None)

    operator_router._dispatch_child_rollout(
        "fleet_x", "dep_a", target_version="2026.07.1",
        callback_url="https://mc/api/rollouts/{rollout_id}/callback", dry_run=True)

    children = store.list_rollouts_for_fleet("fleet_x")
    assert len(children) == 1
    assert children[0].status == "pending"       # created, never marked failed
    assert children[0].exec_status == "pending"  # no dispatch_failed bookkeeping
    assert _RecordingDispatcher.calls == []      # dispatcher never fired


# --- callback router (auth) --------------------------------------------------

def _callback_settings():
    from app.provisioning.runs import hash_callback_secret
    return SimpleNamespace(provisioning_callback_key_hash=hash_callback_secret("s3cret"),
                           provisioning_callback_key_id="kid")


def test_rollout_callback_router_auth_and_drive(monkeypatch):
    store = _control()
    _started(store)
    monkeypatch.setattr(rollouts_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(provisioning_router, "get_settings", _callback_settings)

    with pytest.raises(HTTPException) as ei:  # bad token
        rollouts_router.rollout_callback("roll1", rollouts_router.RolloutCallbackIn(status="running"),
                                         authorization="Bearer nope", x_onebrain_callback_key_id="kid")
    assert ei.value.status_code == 401

    ok = rollouts_router.rollout_callback(
        "roll1", rollouts_router.RolloutCallbackIn(status="running"),
        authorization="Bearer s3cret", x_onebrain_callback_key_id="kid")
    assert ok["exec_status"] == "running" and ok["status"] == "running"


def test_apply_rollout_callback_dry_run_does_not_apply_versions():
    """A dry-run succeeded callback marks the rollout verified but must NOT mutate
    the deployment's version/modules (Railway was never touched)."""
    store = _control()
    _started(store)
    apply_rollout_callback(store, "roll1", RolloutCallback(status="running"))
    r = apply_rollout_callback(store, "roll1", RolloutCallback(status="succeeded", dry_run=True))

    assert r.exec_status == "succeeded" and r.status == "success"  # terminal / verified
    assert store.get_deployment("dep_a").current_version == "2026.07.0"  # UNCHANGED
    assert {m.module_id: m.version for m in store.list_modules("dep_a")}["onebrain-api"] == "0.7.0"


def test_claim_rollout_dispatch_is_single_winner():
    store = _control()
    _started(store)
    assert store.claim_rollout_dispatch("roll1") is True          # first wins
    assert store.get_rollout("roll1").exec_status == "dispatched"
    assert store.claim_rollout_dispatch("roll1") is False         # second loses (not pending)
    assert store.claim_rollout_dispatch("nope") is False          # unknown id

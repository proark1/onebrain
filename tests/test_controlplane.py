"""Operator control plane: releases, customer deployments, backups and rollouts."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.operator as operator_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import (
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    RolloutRun,
)
from app.controlplane.memory import MemoryControlPlaneStore
from app.platform.base import Account
from app.platform.memory import MemoryPlatformStore
from app.servicekeys.base import SCOPE_READ, ServiceKey, hash_secret
from app.servicekeys.memory import MemoryServiceKeyStore


def _admin() -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id="admin@onebrain",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
    )


def _store() -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id="dep_a",
        customer_name="Customer A",
        deployment_type="dedicated_railway",
        release_ring="pilot",
        current_version="2026.07.0",
        current_migration="0041",
    ))
    store.upsert_module(DeploymentModule("dep_a", "onebrain-api", "0.7.0"))
    store.upsert_module(DeploymentModule("dep_a", "communication-api", "0.5.0"))
    return store


def test_update_plan_requires_release_manifest_compatibility():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.1",
        git_sha="abc123",
        modules={"onebrain-api": "0.8.0"},
    ))

    plan = store.plan_update("dep_a", "2026.07.1")

    assert plan.allowed is False
    assert plan.reason == "release_missing_modules:communication-api"
    assert plan.current_modules["communication-api"] == "0.5.0"


def test_schema_update_requires_successful_pre_update_backup():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.2",
        git_sha="def456",
        modules={"onebrain-api": "0.8.0", "communication-api": "0.6.0"},
        migration_from="0041",
        migration_to="0042",
    ))

    no_backup = store.plan_update("dep_a", "2026.07.2")
    assert no_backup.allowed is False
    assert no_backup.reason == "backup_required_for_schema_update"

    store.record_backup(BackupRun("bak_failed", "dep_a", "failed", "snapshot failed"))
    failed_backup = store.plan_update("dep_a", "2026.07.2")
    assert failed_backup.allowed is False

    store.record_backup(BackupRun("bak_success", "dep_a", "success", "snapshot ready"))
    ready = store.plan_update("dep_a", "2026.07.2")
    assert ready.allowed is True
    assert ready.reason == "update_available"
    assert ready.modules_to_update == {"communication-api": "0.6.0", "onebrain-api": "0.8.0"}


def test_rollout_is_blocked_until_update_plan_is_allowed():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.2",
        git_sha="def456",
        modules={"onebrain-api": "0.8.0", "communication-api": "0.6.0"},
        migration_from="0041",
        migration_to="0042",
    ))

    with pytest.raises(ValueError, match="backup_required"):
        store.start_rollout(RolloutRun("roll_1", "dep_a", "2026.07.2", "pending", "admin"))

    store.record_backup(BackupRun("bak_success", "dep_a", "success"))
    rollout = store.start_rollout(RolloutRun("roll_1", "dep_a", "2026.07.2", "pending", "admin"))

    assert rollout.id == "roll_1"
    assert store.list_rollouts("dep_a") == [rollout]


def test_successful_rollout_applies_release_versions_and_migration():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.4",
        git_sha="def999",
        modules={"onebrain-api": "0.9.0", "communication-api": "0.7.0"},
        migration_from="0041",
        migration_to="0042",
    ))
    store.record_backup(BackupRun("bak_success", "dep_a", "success"))
    store.start_rollout(RolloutRun("roll_apply", "dep_a", "2026.07.4", "running", "admin"))

    rollout = store.update_rollout_status("roll_apply", "success", "completed by pipeline")

    assert rollout.status == "success"
    assert rollout.notes == "completed by pipeline"
    assert store.get_deployment("dep_a").current_version == "2026.07.4"
    assert store.get_deployment("dep_a").current_migration == "0042"
    assert {m.module_id: m.version for m in store.list_modules("dep_a")} == {
        "communication-api": "0.7.0",
        "onebrain-api": "0.9.0",
    }
    assert store.plan_update("dep_a", "2026.07.4").reason == "already_current"


def test_failed_rollout_does_not_apply_release_versions():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.4",
        git_sha="def999",
        modules={"onebrain-api": "0.9.0", "communication-api": "0.7.0"},
    ))
    store.start_rollout(RolloutRun("roll_fail", "dep_a", "2026.07.4", "running", "admin"))

    rollout = store.update_rollout_status("roll_fail", "failed", "pipeline failed")

    assert rollout.status == "failed"
    assert store.get_deployment("dep_a").current_version == "2026.07.0"
    assert {m.module_id: m.version for m in store.list_modules("dep_a")} == {
        "communication-api": "0.5.0",
        "onebrain-api": "0.7.0",
    }


def test_terminal_rollout_status_cannot_be_changed():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.4",
        git_sha="def999",
        modules={"onebrain-api": "0.9.0", "communication-api": "0.7.0"},
    ))
    store.start_rollout(RolloutRun("roll_terminal", "dep_a", "2026.07.4", "running", "admin"))
    store.update_rollout_status("roll_terminal", "failed")

    with pytest.raises(ValueError, match="terminal rollout status"):
        store.update_rollout_status("roll_terminal", "running")


def test_rollout_cannot_start_as_success():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.4",
        git_sha="def999",
        modules={"onebrain-api": "0.9.0", "communication-api": "0.7.0"},
    ))

    with pytest.raises(ValueError, match="cannot start as success"):
        store.start_rollout(RolloutRun("roll_skip", "dep_a", "2026.07.4", "success", "admin"))


def test_unknown_module_and_invalid_ring_are_rejected():
    store = MemoryControlPlaneStore()
    with pytest.raises(ValueError, match="Unknown release ring"):
        store.create_deployment(CustomerDeployment(
            id="dep_bad",
            customer_name="Bad",
            release_ring="everyone_now",
        ))

    store.create_deployment(CustomerDeployment(id="dep_ok", customer_name="OK"))
    with pytest.raises(ValueError, match="Unknown module id"):
        store.upsert_module(DeploymentModule("dep_ok", "unknown-module", "1.0.0"))


def test_operator_endpoint_lists_rollout_status(monkeypatch):
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.3",
        git_sha="abc789",
        modules={"onebrain-api": "0.8.0", "communication-api": "0.6.0"},
    ))
    store.start_rollout(RolloutRun("roll_status", "dep_a", "2026.07.3", "running", "admin"))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)

    rollouts = operator_router.list_rollouts("dep_a", principal=_admin())

    assert len(rollouts) == 1
    assert rollouts[0].id == "roll_status"
    assert rollouts[0].status == "running"


def test_operator_endpoint_marks_rollout_success_and_updates_deployment(monkeypatch):
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.5",
        git_sha="abc555",
        modules={"onebrain-api": "1.0.0", "communication-api": "0.8.0"},
    ))
    store.start_rollout(RolloutRun("roll_done", "dep_a", "2026.07.5", "running", "admin"))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)

    rollout = operator_router.update_rollout(
        "roll_done",
        operator_router.RolloutStatusUpdate(status="success", notes="done"),
        principal=_admin(),
    )

    assert rollout.status == "success"
    assert store.get_deployment("dep_a").current_version == "2026.07.5"
    assert {m.module_id: m.version for m in store.list_modules("dep_a")}["onebrain-api"] == "1.0.0"


def test_operator_endpoints_expose_latest_backup_and_health(monkeypatch):
    store = _store()
    store.record_backup(BackupRun("bak_failed", "dep_a", "failed", "old failure"))
    store.record_backup(BackupRun("bak_ready", "dep_a", "success", "pre-update snapshot"))
    store.record_health(HealthCheckRun("hlth_ready", "dep_a", "success", "all checks green"))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)

    backup = operator_router.latest_backup("dep_a", principal=_admin())
    health = operator_router.latest_health("dep_a", principal=_admin())

    assert backup is not None
    assert backup.id == "bak_ready"
    assert backup.status == "success"
    assert health is not None
    assert health.status == "success"


def test_operator_can_list_and_revoke_customer_integration_keys(monkeypatch):
    platform = MemoryPlatformStore()
    keys = MemoryServiceKeyStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme"))
    keys.create(ServiceKey(
        id="key_comm",
        key_hash=hash_secret("secret"),
        tenant_id="acme",
        scopes=(SCOPE_READ,),
        label="Communication integration",
        account_id="acme",
        app_id="communication",
        space_ids=("sp_acme_customer",),
        purposes=("customer_service_answer",),
    ))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)

    listed = operator_router.list_account_service_keys("acme", principal=_admin())

    assert len(listed) == 1
    assert listed[0].id == "key_comm"
    assert listed[0].app_id == "communication"
    assert not hasattr(listed[0], "key")

    revoked = operator_router.revoke_account_service_key("acme", "key_comm", principal=_admin())

    assert revoked == {"revoked": "key_comm"}
    assert keys.get("key_comm").status == "revoked"

    with pytest.raises(HTTPException) as exc:
        operator_router.list_account_service_keys("missing", principal=_admin())
    assert exc.value.status_code == 404

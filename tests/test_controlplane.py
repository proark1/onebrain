"""Operator control plane: releases, customer deployments, backups and rollouts."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

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
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore
from app.jobs.base import JOB_DOCUMENT_INGEST, JOB_SERVICE_CAPTURE
from app.jobs.memory import MemoryJobStore
from app.monitoring import record_api_error, record_auth_failure, reset_monitoring_metrics
from app.controlplane.memory import MemoryControlPlaneStore
from app.platform.base import Account, AppInstallation, Space
from app.platform.memory import MemoryPlatformStore
from app.servicekeys.base import SCOPE_READ, ServiceKey, hash_secret
from app.servicekeys.memory import MemoryServiceKeyStore
from app.store.base import Chunk
from app.store.memory import MemoryStore


def _admin() -> Principal:
    return _principal("admin")


def _principal(role_id: str) -> Principal:
    role = ROLES["admin"]
    if role_id != "admin":
        role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@onebrain",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
    )


def _operator_settings():
    # Mission Control context: operator sees/manages the whole fleet (scoping bypassed).
    return SimpleNamespace(is_operator_surface=True, operator_mode=True)


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
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

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
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

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
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

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
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="admin@onebrain"))
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
    keys.record_usage("key_comm", "service.ask")
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)

    listed = operator_router.list_account_service_keys("acme", principal=_admin())

    assert len(listed) == 1
    assert listed[0].id == "key_comm"
    assert listed[0].app_id == "communication"
    assert listed[0].last_used_endpoint == "service.ask"
    assert listed[0].use_count == 1
    assert not hasattr(listed[0], "key")

    revoked = operator_router.revoke_account_service_key("acme", "key_comm", principal=_admin())

    assert revoked == {"revoked": "key_comm"}
    assert keys.get("key_comm").status == "revoked"
    assert platform.list_audit("acme")[-1].action == "service_key.revoked"
    assert "secret" not in str(platform.list_audit("acme")[-1].meta)

    with pytest.raises(HTTPException) as exc:
        operator_router.list_account_service_keys("missing", principal=_admin())
    assert exc.value.status_code == 404


def test_operator_service_keys_reject_cross_account_admin(monkeypatch):
    """An admin who neither owns nor administers the account cannot list or revoke
    its service keys — same 404 as a missing account, so it can't be enumerated."""
    platform = MemoryPlatformStore()
    keys = MemoryServiceKeyStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="admin@onebrain"))
    keys.create(ServiceKey(
        id="key_comm", key_hash=hash_secret("secret"), tenant_id="acme",
        scopes=(SCOPE_READ,), account_id="acme", app_id="communication",
    ))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)
    outsider = replace(_admin(), user_id="outsider@onebrain")

    with pytest.raises(HTTPException) as listing:
        operator_router.list_account_service_keys("acme", principal=outsider)
    assert listing.value.status_code == 404

    with pytest.raises(HTTPException) as revoked:
        operator_router.revoke_account_service_key("acme", "key_comm", principal=outsider)
    assert revoked.value.status_code == 404
    # The key was NOT revoked despite a well-formed request from another admin.
    assert keys.get("key_comm").status == "active"


def test_operator_customer_overview_aggregates_metadata_only(monkeypatch):
    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    keys = MemoryServiceKeyStore()
    # list_customers now scopes to accounts the admin administers, so make the
    # calling admin the owner (operator-mode-sees-all is covered separately).
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="admin@onebrain"))
    platform.create_space(Space(id="sp_acme_service", account_id="acme", kind="customer_service", name="Service"))
    platform.create_space(Space(id="sp_acme_shared", account_id="acme", kind="shared", name="Shared"))
    platform.install_app(AppInstallation(
        id="appi_acme_comm",
        account_id="acme",
        app_id="communication",
        enabled_space_ids=("sp_acme_service", "sp_acme_shared"),
        allowed_purposes=("customer_service_answer", "customer_service_inbox"),
        display_name="AI Communication",
    ))
    control.create_deployment(CustomerDeployment(
        id="dep_acme",
        customer_name="Acme",
        deployment_type="dedicated_railway",
        release_ring="pilot",
        current_version="2026.07.1",
    ))
    control.upsert_module(DeploymentModule("dep_acme", "onebrain-api", "2026.07.1"))
    control.upsert_module(DeploymentModule("dep_acme", "communication-api", "2026.07.1"))
    control.record_backup(BackupRun("bak_acme", "dep_acme", "success", "ready"))
    control.record_health(HealthCheckRun("hlth_acme", "dep_acme", "success", "ok"))
    keys.create(ServiceKey(
        id="key_comm",
        key_hash=hash_secret("secret"),
        tenant_id="acme",
        scopes=(SCOPE_READ,),
        label="Communication integration",
        account_id="acme",
        app_id="communication",
        space_ids=("sp_acme_service",),
        purposes=("customer_service_answer",),
    ))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)

    rows = operator_router.list_customers(principal=_admin())

    assert len(rows) == 1
    row = rows[0]
    assert row.account.id == "acme"
    assert row.deployment.id == "dep_acme"
    assert row.readiness == "healthy"
    assert {space.kind for space in row.spaces} == {"customer_service", "shared"}
    assert {module.module_id for module in row.modules} == {"onebrain-api", "communication-api"}
    assert row.service_keys[0].id == "key_comm"
    assert not hasattr(row.service_keys[0], "key")


def test_operator_observability_aggregates_current_onebrain_state(monkeypatch):
    reset_monitoring_metrics()
    vector_store = MemoryStore()
    vector_store.add([
        Chunk(
            id="chunk_1",
            doc_id="doc_1",
            text="Internal document text must not appear in observability.",
            meta={"tenant_id": "acme", "account_id": "acme", "space_id": "sp_service"},
        )
    ])
    intake_store = MemoryIntakeStore()
    intake_store.create(IntakeRecord(
        id="rec_1",
        tenant_id="acme",
        account_id="acme",
        space_id="sp_service",
        app_id="communication",
        purpose="customer_service_inbox",
        source="communication",
        source_ref="msg_1",
        record_type="message",
        intent="question",
        classification="internal",
        confidence=0.9,
        status="approved",
        title="Customer question",
        content="Raw intake content must not appear in observability.",
        summary="question",
    ))
    key_store = MemoryServiceKeyStore()
    key_store.create(ServiceKey(
        id="key_active",
        key_hash=hash_secret("secret"),
        tenant_id="acme",
        scopes=(SCOPE_READ,),
    ))
    key_store.create(ServiceKey(
        id="key_revoked",
        key_hash=hash_secret("secret"),
        tenant_id="acme",
        scopes=(SCOPE_READ,),
    ))
    key_store.revoke("key_revoked")
    job_store = MemoryJobStore()
    failed = job_store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="acme",
        account_id="acme",
        space_id="sp_service",
        payload={"raw": "must not appear"},
    )
    job_store.claim("worker_a")
    job_store.mark_failed(failed.id, "embedding provider timeout")
    job_store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="acme")
    monkeypatch.setattr(
        operator_router,
        "get_settings",
        lambda: SimpleNamespace(
            vector_store="memory",
            llm_provider="local",
            embeddings_provider="local",
            environment="local",
            is_operator_surface=True,
            operator_mode=True,
            is_production_like=False,
            database_url="",
            rls_enforced=False,
            cookie_secure=False,
            pii_phase="synthetic",
            top_k=4,
            retrieval_min_score=0.12,
            use_async_ingestion=False,
        ),
    )
    monkeypatch.setattr(operator_router, "get_store", lambda: vector_store)
    monkeypatch.setattr(operator_router, "get_intake_store", lambda: intake_store)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: key_store)
    monkeypatch.setattr(operator_router, "get_job_store", lambda: job_store)
    record_auth_failure("service_key_invalid")
    record_api_error(route="/api/service/intake", status_code=500)

    snapshot = operator_router.operator_observability(principal=_admin())

    assert snapshot.runtime.vector_store == "memory"
    assert snapshot.runtime.async_ingestion is False
    assert snapshot.retrieval.top_k == 4
    assert snapshot.retrieval.min_score == 0.12
    assert snapshot.storage.chunks == 1
    assert snapshot.storage.intake_records == 1
    assert snapshot.service_keys.total == 2
    assert snapshot.service_keys.active == 1
    assert snapshot.service_keys.revoked == 1
    assert snapshot.jobs.total == 2
    assert snapshot.jobs.by_status["failed"] == 1
    assert snapshot.jobs.by_status["queued"] == 1
    assert snapshot.jobs.by_type[JOB_DOCUMENT_INGEST] == 1
    assert snapshot.security.production_like is False
    assert snapshot.security.pii_phase == "synthetic"
    assert snapshot.worker.failed_jobs == 1
    assert snapshot.worker.pending_jobs == 1
    assert snapshot.worker.status == "not_required"
    assert snapshot.auth.service_key_failures == 1
    assert snapshot.auth.total_failures == 1
    assert snapshot.api.errors_5xx == 1
    assert snapshot.api.last_error_route == "/api/service/intake"
    assert {alert.id for alert in snapshot.alerts} == {"api-errors", "auth-failures", "job-failures"}
    failure = snapshot.jobs.recent_failures[0]
    assert failure.id == failed.id
    assert failure.error == "embedding provider timeout"
    dumped = snapshot.model_dump()
    assert "payload" not in dumped["jobs"]["recent_failures"][0]
    assert "result" not in dumped["jobs"]["recent_failures"][0]
    assert "Internal document text" not in str(dumped)
    assert "Raw intake content" not in str(dumped)
    assert "secret" not in str(dumped)


def test_operator_observability_requires_admin():
    with pytest.raises(HTTPException) as exc:
        operator_router.operator_observability(principal=_principal("front_desk"))
    assert exc.value.status_code == 403


# --- operator surface hardening ---------------------------------------------

def test_list_customers_scopes_to_administered_accounts(monkeypatch):
    """A non-owning admin must not enumerate another account's metadata; the
    owning admin still sees theirs. (Operator-mode-sees-all covered separately.)"""
    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    keys = MemoryServiceKeyStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="owner@acme"))
    platform.create_account(Account(id="beta", kind="organization", name="Beta", owner_user_id="admin@onebrain"))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)

    rows = operator_router.list_customers(principal=_principal("admin"))  # user_id admin@onebrain

    assert {r.account.id for r in rows} == {"beta"}  # not acme


def test_list_customers_operator_mode_sees_all(monkeypatch):
    from types import SimpleNamespace

    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    keys = MemoryServiceKeyStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="owner@acme"))
    platform.create_account(Account(id="beta", kind="organization", name="Beta", owner_user_id="owner@beta"))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: SimpleNamespace(is_operator_surface=True, operator_mode=True))

    rows = operator_router.list_customers(principal=_principal("admin"))
    assert {r.account.id for r in rows} == {"acme", "beta"}  # operator sees the whole fleet


def test_operator_admin_refused_when_not_operator_surface(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: SimpleNamespace(is_operator_surface=False))
    with pytest.raises(HTTPException) as ei:
        operator_router.list_releases(principal=_principal("admin"))
    assert ei.value.status_code == 404  # surface must not serve on a customer stack


# --- per-deployment cross-account scoping ------------------------------------

def _scoping_stores(owner="owner@acme"):
    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id=owner))
    control.create_deployment(CustomerDeployment(id="dep_acme", customer_name="Acme", release_ring="manual"))
    control.upsert_module(DeploymentModule("dep_acme", "onebrain-api", "1.0.0"))
    control.record_backup(BackupRun("bak", "dep_acme", "success"))
    control.record_health(HealthCheckRun("hlth", "dep_acme", "success"))
    return platform, control


def test_per_deployment_reads_reject_non_owning_admin(monkeypatch):
    platform, control = _scoping_stores(owner="someone_else@acme")
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    # Real settings: operator_mode=False, so account scoping is enforced.
    outsider = _principal("admin")  # admin@onebrain, does NOT own acme

    for call in (
        lambda: operator_router.list_modules("dep_acme", principal=outsider),
        lambda: operator_router.latest_backup("dep_acme", principal=outsider),
        lambda: operator_router.latest_health("dep_acme", principal=outsider),
        lambda: operator_router.update_plan("dep_acme", "9.9.9", principal=outsider),
        lambda: operator_router.list_rollouts("dep_acme", principal=outsider),
    ):
        with pytest.raises(HTTPException) as ei:
            call()
        assert ei.value.status_code == 404


def test_per_deployment_reads_allow_owning_admin(monkeypatch):
    platform, control = _scoping_stores(owner="admin@onebrain")  # caller owns acme
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    owner = _principal("admin")

    assert [m.module_id for m in operator_router.list_modules("dep_acme", principal=owner)] == ["onebrain-api"]
    assert operator_router.latest_backup("dep_acme", principal=owner).status == "success"


def test_list_deployments_filters_to_administered(monkeypatch):
    platform, control = _scoping_stores(owner="someone_else@acme")
    control.create_deployment(CustomerDeployment(id="dep_beta", customer_name="Beta", release_ring="manual"))
    platform.create_account(Account(id="beta", kind="organization", name="Beta", owner_user_id="admin@onebrain"))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)

    rows = operator_router.list_deployments(principal=_principal("admin"))  # owns beta only
    assert {d.id for d in rows} == {"dep_beta"}


def test_deployment_authz_ignores_account_name_collision(monkeypatch):
    """An attacker who mints an account named after a victim deployment's
    customer_name must NOT gain access — authorization uses the deterministic
    dep_{account_id}/audit mapping, never the display name heuristic."""
    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    platform.create_account(Account(id="realowner", kind="organization", name="Real",
                                    owner_user_id="real@x"))
    # Deployment owned by realowner via the dep_{account_id} convention, but whose
    # display customer_name matches the attacker's account name.
    control.create_deployment(CustomerDeployment(id="dep_realowner", customer_name="Attacker Inc",
                                                 release_ring="manual"))
    control.upsert_module(DeploymentModule("dep_realowner", "onebrain-api", "1.0.0"))
    platform.create_account(Account(id="attacker", kind="organization", name="Attacker Inc",
                                    owner_user_id="attacker@x"))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)

    attacker = _principal("admin")
    object.__setattr__(attacker, "user_id", "attacker@x")  # admin who owns only 'attacker'
    with pytest.raises(HTTPException) as ei:
        operator_router.list_modules("dep_realowner", principal=attacker)
    assert ei.value.status_code == 404


# --- authoritative account_id linkage ----------------------------------------

def test_create_deployment_requires_account_admin(monkeypatch):
    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="owner@acme"))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)

    with pytest.raises(HTTPException) as ei:  # non-owning admin cannot create for acme
        operator_router.create_deployment(
            operator_router.DeploymentCreate(customer_name="Acme", account_id="acme", id="dep_x"),
            principal=_principal("admin"))
    assert ei.value.status_code == 404

    owner = _principal("admin")
    object.__setattr__(owner, "user_id", "owner@acme")
    operator_router.create_deployment(
        operator_router.DeploymentCreate(customer_name="Acme", account_id="acme", id="dep_x"), principal=owner)
    assert control.get_deployment("dep_x").account_id == "acme"


def test_authorize_deployment_prefers_account_id_over_convention(monkeypatch):
    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="acme_admin@x"))
    platform.create_account(Account(id="realowner", kind="organization", name="Real", owner_user_id="real@x"))
    # id follows the dep_acme convention, but account_id authoritatively binds realowner.
    control.create_deployment(CustomerDeployment(id="dep_acme", customer_name="X",
                                                 account_id="realowner", release_ring="manual"))
    control.upsert_module(DeploymentModule("dep_acme", "onebrain-api", "1.0.0"))
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)

    acme_admin = _principal("admin")
    object.__setattr__(acme_admin, "user_id", "acme_admin@x")  # matches the convention, but field wins
    with pytest.raises(HTTPException) as ei:
        operator_router.list_modules("dep_acme", principal=acme_admin)
    assert ei.value.status_code == 404

    real = _principal("admin")
    object.__setattr__(real, "user_id", "real@x")
    assert [m.module_id for m in operator_router.list_modules("dep_acme", principal=real)] == ["onebrain-api"]

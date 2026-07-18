"""Operator control plane: releases, customer deployments, backups and rollouts."""

from __future__ import annotations

import json
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
    compute_update_plan,
    effective_update_policy,
)
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore
from app.jobs.base import JOB_DOCUMENT_INGEST, JOB_SERVICE_CAPTURE
from app.jobs.memory import MemoryJobStore
from app.monitoring import record_api_error, record_auth_failure, reset_monitoring_metrics
from app.controlplane.memory import MemoryControlPlaneStore
from app.trust.release import (
    release_signature_fields,
    release_signature_fields_from_body,
    sign_release,
    verify_release_signature,
)
from app.trust.signing import generate_keypair
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


def _operator_settings(**over):
    # Mission Control context: operator sees/manages the whole fleet (scoping
    # bypassed). Carries the release trust keys (spec §2 defaults — all inert)
    # plus the dispatch plumbing keys so endpoint tests share one helper.
    data = dict(
        is_operator_surface=True, operator_mode=True,
        release_verify_public_key="",
        release_require_signature=False,
        release_require_signed_images=False,
        release_require_rollback_kind=False,
        release_registry_allowlist="ghcr.io/proark1",
        provisioning_callback_key_id="",
        provisioning_callback_allowed_hosts="",
    )
    data.update(over)
    return SimpleNamespace(**data)


@pytest.fixture(autouse=True)
def _mount_operator_surface(monkeypatch):
    """Direct router tests exercise Mission Control, never the secure customer default."""
    monkeypatch.setattr(
        operator_router,
        "get_settings",
        lambda: _operator_settings(operator_mode=False),
    )


def _store() -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id="dep_a",
        customer_name="Customer A",
        deployment_type="dedicated_server",
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


def test_selected_product_module_ids_round_trip_in_memory_store(tmp_path):
    path = tmp_path / "controlplane.json"
    store = MemoryControlPlaneStore(persist_path=str(path))
    created = store.create_deployment(CustomerDeployment(
        id="dep_modules",
        customer_name="Module Co",
        selected_module_ids=("assistant", "kpi_dashboard"),
    ))

    assert created.selected_module_ids == ("assistant", "kpi_dashboard")
    reloaded = MemoryControlPlaneStore(persist_path=str(path))
    assert reloaded.get_deployment("dep_modules").selected_module_ids == ("assistant", "kpi_dashboard")

    with pytest.raises(ValueError, match="duplicates"):
        store.create_deployment(CustomerDeployment(
            id="dep_duplicate_modules",
            customer_name="Duplicate Co",
            selected_module_ids=("assistant", "assistant"),
        ))


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


def test_operator_rollout_responses_include_lifecycle_timestamps_and_safe_execution_detail(monkeypatch):
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.4",
        git_sha="abc456",
        modules={"onebrain-api": "0.8.0", "communication-api": "0.6.0"},
    ))
    store.start_rollout(RolloutRun(
        "roll_lifecycle",
        "dep_a",
        "2026.07.4",
        "pending",
        "admin",
        notes="Waiting for the development gate.",
        created_at="2026-07-17T10:00:00+00:00",
    ))
    store.update_rollout_exec(
        "roll_lifecycle",
        exec_status="failed",
        external_run_id="run_123",
        external_run_url="https://fleet.example/rollouts/123",
        failure_reason="The deployment did not report healthy.",
        dispatched_at="2026-07-17T10:01:00+00:00",
        completed_at="2026-07-17T10:04:00+00:00",
    )
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    rollout = operator_router.list_rollouts("dep_a", principal=_admin())[0]

    assert rollout.created_at == "2026-07-17T10:00:00+00:00"
    assert rollout.exec_status == "failed"
    assert rollout.external_provider == "hetzner"
    assert rollout.external_run_id == "run_123"
    assert rollout.external_run_url.endswith("/123")
    assert rollout.failure_reason == "The deployment did not report healthy."
    assert rollout.dispatched_at == "2026-07-17T10:01:00+00:00"
    assert rollout.completed_at == "2026-07-17T10:04:00+00:00"


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
    store.record_backup(BackupRun(
        "bak_failed", "dep_a", "failed", "old failure", created_at="2026-07-17T09:00:00+00:00",
    ))
    store.record_backup(BackupRun(
        "bak_ready", "dep_a", "success", "pre-update snapshot", created_at="2026-07-17T09:10:00+00:00",
    ))
    store.record_health(HealthCheckRun(
        "hlth_ready", "dep_a", "success", "all checks green", created_at="2026-07-17T09:11:00+00:00",
    ))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    backup = operator_router.latest_backup("dep_a", principal=_admin())
    health = operator_router.latest_health("dep_a", principal=_admin())

    assert backup is not None
    assert backup.id == "bak_ready"
    assert backup.status == "success"
    assert backup.detail == "pre-update snapshot"
    assert backup.created_at == "2026-07-17T09:10:00+00:00"
    assert health is not None
    assert health.status == "success"
    assert health.detail == "all checks green"
    assert health.created_at == "2026-07-17T09:11:00+00:00"


def test_control_plane_lists_releases_and_rollouts_newest_first(monkeypatch):
    store = _store()
    for version, created_at in (
        ("2026.07.8", "2026-07-17T08:00:00+00:00"),
        ("2026.07.10", "2026-07-17T10:00:00+00:00"),
        ("2026.07.11", "2026-07-17T10:00:00+00:00"),
    ):
        store.create_release(ReleaseManifest(
            version=version,
            git_sha=f"sha-{version}",
            modules={"onebrain-api": "0.8.0", "communication-api": "0.6.0"},
            created_at=created_at,
        ))
    for rollout_id, version, created_at in (
        ("roll_old", "2026.07.8", "2026-07-17T08:10:00+00:00"),
        ("roll_same_time_a", "2026.07.10", "2026-07-17T10:10:00+00:00"),
        ("roll_same_time_b", "2026.07.11", "2026-07-17T10:10:00+00:00"),
    ):
        store.start_rollout(RolloutRun(
            rollout_id,
            "dep_a",
            version,
            "failed",
            "admin",
            created_at=created_at,
        ))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    assert [release.version for release in store.list_releases()] == [
        "2026.07.11", "2026.07.10", "2026.07.8",
    ]
    assert [rollout.id for rollout in store.list_rollouts("dep_a")] == [
        "roll_same_time_b", "roll_same_time_a", "roll_old",
    ]
    assert [release.version for release in operator_router.list_releases(principal=_admin())] == [
        "2026.07.11", "2026.07.10", "2026.07.8",
    ]



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
        deployment_type="dedicated_server",
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
    claimed = job_store.claim("worker_a")
    job_store.mark_failed(
        failed.id,
        "embedding provider timeout",
        lease_token=claimed[0].lease_token,
    )
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


def test_operator_release_response_includes_creation_date_and_images():
    release = ReleaseManifest(
        version="2026.07.13",
        git_sha="abc123",
        modules={"onebrain-api": "2026.07.13"},
        status="active",
        created_at="2026-07-13T10:30:00+00:00",
        images={"onebrain-api": "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64},
    )

    response = operator_router._release_out(release)

    assert response.created_at == "2026-07-13T10:30:00+00:00"
    assert response.images == release.images


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


# --- Hetzner P0 trust primitives (WP1): data model v2 + hoisted plan gate -----

_DIGEST = "a" * 64
_IMAGES = {
    "onebrain-api": f"ghcr.io/proark1/onebrain-api@sha256:{_DIGEST}",
    "communication-api": f"ghcr.io/proark1/communication-api@sha256:{_DIGEST}",
}
_MODULES = {"onebrain-api": "0.8.0", "communication-api": "0.6.0"}


def test_release_round_trips_images_and_rollback_kind():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.6",
        git_sha="abc123",
        modules=dict(_MODULES),
        images=dict(_IMAGES),
        rollback_kind="code_only",
        signature="c2lnbmF0dXJl",
        signing_key_id="release-key-2026",
    ))

    release = store.get_release("2026.07.6")

    assert release.images == _IMAGES
    assert release.rollback_kind == "code_only"
    assert release.signature == "c2lnbmF0dXJl"
    assert release.signing_key_id == "release-key-2026"


def test_release_rejects_non_digest_images():
    store = _store()
    # Floating tag.
    with pytest.raises(ValueError, match="not digest-pinned"):
        store.create_release(ReleaseManifest(
            version="v1", git_sha="a", modules={"onebrain-api": "1.0"},
            images={"onebrain-api": "ghcr.io/x/y:latest"},
        ))
    # Digestless ref.
    with pytest.raises(ValueError, match="not digest-pinned"):
        store.create_release(ReleaseManifest(
            version="v2", git_sha="a", modules={"onebrain-api": "1.0"},
            images={"onebrain-api": "ghcr.io/x/y"},
        ))
    # Unknown module key in the images map.
    with pytest.raises(ValueError, match="cover exactly"):
        store.create_release(ReleaseManifest(
            version="v3", git_sha="a", modules={"onebrain-api": "1.0"},
            images={"unknown-module": f"ghcr.io/x/y@sha256:{_DIGEST}"},
        ))
    # images keys != modules keys.
    with pytest.raises(ValueError, match="cover exactly"):
        store.create_release(ReleaseManifest(
            version="v4", git_sha="a", modules={"onebrain-api": "1.0"},
            images=dict(_IMAGES),
        ))
    # Unknown rollback kind.
    with pytest.raises(ValueError, match="Unknown rollback kind"):
        store.create_release(ReleaseManifest(
            version="v5", git_sha="a", modules={"onebrain-api": "1.0"},
            rollback_kind="who_knows",
        ))


def test_release_without_images_still_valid():
    store = _store()
    release = store.create_release(ReleaseManifest(
        version="2026.07.7", git_sha="legacy", modules=dict(_MODULES),
    ))

    assert release.images == {}
    assert release.rollback_kind == ""
    assert store.plan_update("dep_a", "2026.07.7").allowed is True


def test_deployment_update_policy_round_trip_and_validation():
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id="dep_pinned", customer_name="Pinned Co", update_policy="pinned",
    ))
    assert store.get_deployment("dep_pinned").update_policy == "pinned"

    with pytest.raises(ValueError, match="Unknown update policy"):
        store.create_deployment(CustomerDeployment(
            id="dep_bad", customer_name="Bad", update_policy="whenever",
        ))

    updated = store.set_update_policy("dep_pinned", "manual")
    assert updated.update_policy == "manual"
    assert store.get_deployment("dep_pinned").update_policy == "manual"

    with pytest.raises(ValueError, match="Unknown update policy"):
        store.set_update_policy("dep_pinned", "")
    with pytest.raises(ValueError, match="Unknown update policy"):
        store.set_update_policy("dep_pinned", "yolo")
    with pytest.raises(ValueError, match="unknown deployment"):
        store.set_update_policy("dep_missing", "auto")


def test_effective_update_policy_fallback():
    manual_ring = CustomerDeployment(id="d1", customer_name="c", release_ring="manual")
    pilot_ring = CustomerDeployment(id="d2", customer_name="c", release_ring="pilot")
    explicit = CustomerDeployment(id="d3", customer_name="c", release_ring="manual",
                                  update_policy="pinned")

    assert effective_update_policy(manual_ring) == "manual"
    assert effective_update_policy(pilot_ring) == "auto"
    assert effective_update_policy(explicit) == "pinned"
    # getattr-based: SimpleNamespace fakes without update_policy fall back to the ring.
    assert effective_update_policy(SimpleNamespace(release_ring="manual")) == "manual"
    assert effective_update_policy(SimpleNamespace(release_ring="stable")) == "auto"


def test_plan_blocks_pinned_policy():
    store = _store()
    store.create_release(ReleaseManifest(version="2026.07.8", git_sha="a", modules=dict(_MODULES)))
    store.set_update_policy("dep_a", "pinned")

    plan = store.plan_update("dep_a", "2026.07.8")

    assert plan.allowed is False
    assert plan.reason == "update_policy_pinned"

    # A pinned deployment may still plan its own current version.
    store.create_release(ReleaseManifest(
        version="2026.07.0", git_sha="b",
        modules={"onebrain-api": "0.7.0", "communication-api": "0.5.0"},
    ))
    assert store.plan_update("dep_a", "2026.07.0").allowed is True


def test_plan_blocks_yanked_release():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.9", git_sha="a", modules=dict(_MODULES), status="yanked",
    ))

    plan = store.plan_update("dep_a", "2026.07.9")

    assert plan.allowed is False
    assert plan.reason == "release_yanked"


def test_plan_restore_required_needs_ack():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.10", git_sha="a", modules=dict(_MODULES),
        rollback_kind="restore_required",
    ))
    # B6: restore_required always fires the backup gate — seed a fresh backup.
    store.record_backup(BackupRun("bak_rr", "dep_a", "success"))

    blocked = store.plan_update("dep_a", "2026.07.10")
    assert blocked.allowed is False
    assert blocked.reason == "restore_required_ack_needed"

    acked = store.plan_update("dep_a", "2026.07.10", ack_restore_required=True)
    assert acked.allowed is True

    # A manual-policy deployment is only updated deliberately — no ack needed.
    store.set_update_policy("dep_a", "manual")
    manual = store.plan_update("dep_a", "2026.07.10")
    assert manual.allowed is True


def test_plan_reports_rollback_kind():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.11", git_sha="a", modules=dict(_MODULES),
        rollback_kind="code_only",
    ))

    plan = store.plan_update("dep_a", "2026.07.11")

    assert plan.allowed is True
    assert plan.rollback_kind == "code_only"


def test_start_rollout_carries_ack():
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.12", git_sha="a", modules=dict(_MODULES),
        rollback_kind="restore_required",
    ))
    store.record_backup(BackupRun("bak_ack", "dep_a", "success"))

    with pytest.raises(ValueError, match="rollout blocked: restore_required_ack_needed"):
        store.start_rollout(RolloutRun("roll_noack", "dep_a", "2026.07.12", "running", "admin"))

    rollout = store.start_rollout(RolloutRun(
        "roll_ack", "dep_a", "2026.07.12", "running", "admin", ack_restore_required=True,
    ))
    assert rollout.ack_restore_required is True

    # The success-apply re-check reuses the persisted ack.
    done = store.update_rollout_status("roll_ack", "success")
    assert done.status == "success"
    assert store.get_deployment("dep_a").current_version == "2026.07.12"


def test_plan_requires_backup_for_restore_required():
    store = _store()
    # migration_to EQUALS current_migration (comm-only destructive DDL): the
    # migration condition alone would never fire — B6's kind condition must.
    store.create_release(ReleaseManifest(
        version="2026.07.13", git_sha="a", modules=dict(_MODULES),
        migration_from="0041", migration_to="0041",
        rollback_kind="restore_required",
    ))

    blocked = store.plan_update("dep_a", "2026.07.13", ack_restore_required=True)
    assert blocked.allowed is False
    assert blocked.reason == "backup_required_for_schema_update"

    store.record_backup(BackupRun("bak_b6", "dep_a", "success"))
    assert store.plan_update("dep_a", "2026.07.13", ack_restore_required=True).allowed is True

    # Dormancy: a legacy release (kind '', no migration change) never hits the gate.
    store2 = _store()
    store2.create_release(ReleaseManifest(version="2026.07.14", git_sha="a", modules=dict(_MODULES)))
    assert store2.plan_update("dep_a", "2026.07.14").allowed is True


def test_plan_backup_lookup_is_lazy():
    deployment = CustomerDeployment(id="dep_l", customer_name="Lazy", release_ring="pilot")
    release = ReleaseManifest(version="1.0", git_sha="a", modules={"onebrain-api": "1.0"})
    modules = [DeploymentModule("dep_l", "onebrain-api", "0.9")]

    plan = compute_update_plan(
        "dep_l", "1.0",
        deployment=deployment,
        release=release,
        modules=modules,
        latest_backup=lambda: pytest.fail("latest_backup must not be called on a no-gate plan"),
    )

    assert plan.allowed is True
    assert plan.modules_to_update == {"onebrain-api": "1.0"}


def test_plan_blocks_unsigned_release_when_required(monkeypatch):
    deployment = CustomerDeployment(id="dep_s", customer_name="Signed", release_ring="pilot")
    unsigned = ReleaseManifest(version="1.0", git_sha="a", modules={"onebrain-api": "1.0"})
    modules = [DeploymentModule("dep_s", "onebrain-api", "0.9")]

    blocked = compute_update_plan(
        "dep_s", "1.0", deployment=deployment, release=unsigned, modules=modules,
        latest_backup=lambda: None, require_signed_release=True,
    )
    assert blocked.allowed is False
    assert blocked.reason == "release_unsigned"

    signed = replace(unsigned, signature="c2ln")
    allowed = compute_update_plan(
        "dep_s", "1.0", deployment=deployment, release=signed, modules=modules,
        latest_backup=lambda: None, require_signed_release=True,
    )
    assert allowed.allowed is True

    # Store level (C2): the flag reaches the stores via app.config.get_settings —
    # patching operator_router.get_settings would NOT reach require_signed_releases().
    import app.config as app_config

    monkeypatch.setattr(app_config, "get_settings",
                        lambda: SimpleNamespace(release_require_signature=True))
    store = _store()
    store.create_release(ReleaseManifest(version="2026.07.15", git_sha="a", modules=dict(_MODULES)))
    plan = store.plan_update("dep_a", "2026.07.15")
    assert plan.allowed is False
    assert plan.reason == "release_unsigned"


def test_success_apply_recheck_blocks_midflight_yank():
    store = _store()
    store.create_release(ReleaseManifest(version="2026.07.16", git_sha="a", modules=dict(_MODULES)))
    store.start_rollout(RolloutRun("roll_yank", "dep_a", "2026.07.16", "running", "admin"))

    release = store.get_release("2026.07.16")
    store._releases["2026.07.16"] = replace(release, status="yanked")

    with pytest.raises(ValueError, match="rollout completion blocked: release_yanked"):
        store.update_rollout_status("roll_yank", "success")


def test_success_apply_recheck_blocks_midflight_pin():
    store = _store()
    store.create_release(ReleaseManifest(version="2026.07.17", git_sha="a", modules=dict(_MODULES)))
    store.start_rollout(RolloutRun("roll_pin", "dep_a", "2026.07.17", "running", "admin"))

    store.set_update_policy("dep_a", "pinned")

    with pytest.raises(ValueError, match="rollout completion blocked: update_policy_pinned"):
        store.update_rollout_status("roll_pin", "success")


def test_memory_store_loads_pre_0019_json(tmp_path):
    """A persistence file written before 0019 (no images/rollback_kind/signature/
    update_policy/ack keys) must load — _load wipes everything on error."""
    persist = tmp_path / "controlplane.json"
    persist.write_text(json.dumps({
        "deployments": [{
            "id": "dep_old", "customer_name": "Old Co", "account_id": "",
            "environment": "production", "deployment_type": "dedicated_railway",
            "region": "", "release_ring": "manual", "status": "active",
            "current_version": "2026.06.0", "current_migration": "0040",
            "created_at": "2026-06-01T00:00:00+00:00",
        }],
        "modules": [{
            "deployment_id": "dep_old", "module_id": "onebrain-api",
            "version": "0.6.0", "status": "active",
        }],
        "releases": [{
            "version": "2026.06.0", "git_sha": "old", "modules": {"onebrain-api": "0.6.0"},
            "migration_from": "", "migration_to": "", "security_notes": "",
            "rollback_plan": "", "status": "draft", "created_at": "",
        }],
        "backups": [],
        "health": [],
        "rollouts": [{
            "id": "roll_old", "deployment_id": "dep_old", "target_version": "2026.06.0",
            "status": "success", "started_by": "admin", "notes": "", "created_at": "",
            "exec_status": "completed", "external_provider": "github_actions",
            "external_run_id": "", "external_run_url": "", "failure_reason": "",
            "request_payload": {}, "dispatched_at": "", "completed_at": "",
            "fleet_rollout_id": "",
        }],
        "fleet_rollouts": [],
    }), encoding="utf-8")

    store = MemoryControlPlaneStore(persist_path=str(persist))

    deployment = store.get_deployment("dep_old")
    assert deployment is not None
    assert deployment.update_policy == ""
    release = store.get_release("2026.06.0")
    assert release is not None
    assert release.images == {}
    assert release.rollback_kind == ""
    rollout = store.get_rollout("roll_old")
    assert rollout is not None
    assert rollout.ack_restore_required is False
    # Legacy rows plan exactly as before.
    assert store.plan_update("dep_old", "2026.06.0").reason == "already_current"


# --- Hetzner P0 trust primitives (WP4): operator-surface gates ------------------

def test_create_release_endpoint_accepts_and_returns_images(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    out = operator_router.create_release(operator_router.ReleaseCreate(
        version="2026.07.18", git_sha="abc123", modules=dict(_MODULES),
        images=dict(_IMAGES), rollback_kind="code_only",
    ), principal=_admin())

    assert out.images == _IMAGES
    assert out.rollback_kind == "code_only"
    stored = store.get_release("2026.07.18")
    assert stored.images == _IMAGES
    assert stored.rollback_kind == "code_only"


def test_create_release_endpoint_rejects_off_allowlist_registry(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: _operator_settings(release_registry_allowlist="ghcr.io/proark1"))

    for ref in (
        f"docker.io/x/y@sha256:{_DIGEST}",
        f"ghcr.io/otherorg/x@sha256:{_DIGEST}",  # same host, different org — the B2 case
    ):
        body = operator_router.ReleaseCreate(
            version="v_allow", git_sha="a", modules={"onebrain-api": "1.0"},
            images={"onebrain-api": ref})
        with pytest.raises(HTTPException) as ei:
            operator_router.create_release(body, principal=_admin())
        assert ei.value.status_code == 400
        assert "not in registry allowlist" in ei.value.detail
    assert store.get_release("v_allow") is None  # never persisted


def test_create_release_endpoint_verifies_signature_when_present(monkeypatch):
    store = _store()
    private_key, public_key = generate_keypair()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: _operator_settings(release_verify_public_key=public_key))

    body = operator_router.ReleaseCreate(
        version="2026.07.20", git_sha="abc123", modules=dict(_MODULES),
        images=dict(_IMAGES), rollback_kind="code_only",
    )
    signature = sign_release(release_signature_fields_from_body(body), private_key)
    out = operator_router.create_release(
        body.model_copy(update={"signature": signature}), principal=_admin())
    assert out.signature == signature

    # A tampered field no longer matches the signed payload — refused even
    # though no release_require_* flag is on (a present signature is always
    # verified; a bad one must never be stored as if good).
    tampered = body.model_copy(update={"git_sha": "abc124", "signature": signature})
    with pytest.raises(HTTPException) as ei:
        operator_router.create_release(tampered, principal=_admin())
    assert ei.value.status_code == 400
    assert "release signature verification failed" in ei.value.detail


def test_signed_release_reverifies_from_stored_row(monkeypatch):
    """A6: the signature is computed over the STRIPPED values the endpoint
    persists, so the STORED row re-verifies — not just the raw request body."""
    store = _store()
    private_key, public_key = generate_keypair()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: _operator_settings(release_verify_public_key=public_key))

    body = operator_router.ReleaseCreate(
        version=" 2026.07.22 ",
        git_sha=" abc123 ",
        modules={" onebrain-api ": " 0.8.0 ", "communication-api": "0.6.0"},
        images={" onebrain-api ": f" {_IMAGES['onebrain-api']} ",
                "communication-api": _IMAGES["communication-api"]},
        migration_from=" 0041 ",
        migration_to=" 0041 ",
        rollback_kind=" code_only ",
    )
    signature = sign_release(release_signature_fields_from_body(body), private_key)

    operator_router.create_release(
        body.model_copy(update={"signature": signature}), principal=_admin())

    release = store.get_release("2026.07.22")
    assert release is not None
    assert release.git_sha == "abc123"
    assert release.images == _IMAGES
    assert verify_release_signature(
        release_signature_fields(release), release.signature, public_key) is True


def test_create_release_endpoint_requires_signature_when_flag_on(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings",
                        lambda: _operator_settings(release_require_signature=True))
    body = operator_router.ReleaseCreate(version="2026.07.23", git_sha="a", modules=dict(_MODULES))

    with pytest.raises(HTTPException) as ei:
        operator_router.create_release(body, principal=_admin())
    assert ei.value.status_code == 400
    assert "release signature is required" in ei.value.detail

    # Dormancy: all flags off, no signature -> still creates (today's flow).
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    out = operator_router.create_release(body, principal=_admin())
    assert out.signature == ""


def test_create_release_flags_off_still_enforces_allowlist(monkeypatch):
    """C7 / ground rule 1: supplying an images map is itself the opt-in — with
    EVERY release_require_* flag off, an off-allowlist image still 400s. There
    is deliberately no enforcement flag for the allowlist."""
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)  # all flags off

    body = operator_router.ReleaseCreate(
        version="v_c7", git_sha="a", modules={"onebrain-api": "1.0"},
        images={"onebrain-api": f"docker.io/evil/onebrain-api@sha256:{_DIGEST}"})
    with pytest.raises(HTTPException) as ei:
        operator_router.create_release(body, principal=_admin())
    assert ei.value.status_code == 400
    assert "not in registry allowlist" in ei.value.detail


def test_set_update_policy_endpoint_authz_and_validation(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    with pytest.raises(HTTPException) as ei:  # non-admin refused
        operator_router.set_update_policy(
            "dep_a", operator_router.UpdatePolicyUpdate(update_policy="manual"),
            principal=_principal("front_desk"))
    assert ei.value.status_code == 403

    with pytest.raises(HTTPException) as ei:  # bad vocabulary
        operator_router.set_update_policy(
            "dep_a", operator_router.UpdatePolicyUpdate(update_policy="yolo"),
            principal=_admin())
    assert ei.value.status_code == 400

    out = operator_router.set_update_policy(
        "dep_a", operator_router.UpdatePolicyUpdate(update_policy="pinned"),
        principal=_admin())
    assert out.update_policy == "pinned"
    assert store.get_deployment("dep_a").update_policy == "pinned"


def test_dispatch_requires_ack_for_restore_required(monkeypatch):
    """D-10 at the dispatch gate. WITH-ack half: an acked rollout against a
    restore_required release on an auto deployment dispatches past the plan
    gate. Ack-less half (A5): start_rollout re-runs the plan gate, so an
    ack-less rollout on an AUTO deployment can never be CREATED directly — the
    only in-model route is start under manual policy, flip to auto, dispatch."""
    import app.routers.provisioning as provisioning_router

    def _restore_required_store():
        s = _store()
        s.create_release(ReleaseManifest(
            version="2026.07.24", git_sha="a", modules=dict(_MODULES),
            rollback_kind="restore_required"))
        s.record_backup(BackupRun("bak_disp", "dep_a", "success"))  # B6
        return s

    prov_runs = [SimpleNamespace(
        id="r1", deployment_id="dep_a", status="succeeded", railway_project_id="hetzner:123",
        railway_environment_id="e1", result_payload={"service_ids": {}},
        completed_at="t", created_at="t")]

    class _ProvStore:
        def list_runs(self, account_id="", deployment_id=""):
            return [r for r in prov_runs if not deployment_id or r.deployment_id == deployment_id]

    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    monkeypatch.setattr(provisioning_router, "get_settings", _operator_settings)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: _ProvStore())
    body = operator_router.RolloutDispatch(
        callback_url="https://mc.example/api/rollouts/{rollout_id}/callback", dry_run=True)

    # WITH ack: the Hetzner pull target is offered.
    store = _restore_required_store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    store.start_rollout(RolloutRun("roll_acked", "dep_a", "2026.07.24", "pending", "op",
                                   ack_restore_required=True))
    out = operator_router.dispatch_rollout("dep_a", "roll_acked", body, principal=_admin())
    assert out.ack_restore_required is True
    assert store.get_rollout("roll_acked").exec_status == "dispatched"
    assert store.get_rollout("roll_acked").request_payload == {
        "provider": "hetzner",
        "pull": True,
        "target_source": "provisioning_run",
    }

    # Ack-less (A5 construction — do not improvise another path).
    store2 = _restore_required_store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store2)
    store2.set_update_policy("dep_a", "manual")
    store2.start_rollout(RolloutRun("roll_noack", "dep_a", "2026.07.24", "pending", "op"))
    store2.set_update_policy("dep_a", "auto")

    with pytest.raises(HTTPException) as ei:
        operator_router.dispatch_rollout("dep_a", "roll_noack", body, principal=_admin())
    assert ei.value.status_code == 409
    assert "restore_required_ack_needed" in ei.value.detail
    assert store2.get_rollout("roll_noack").exec_status == "pending"  # plan-blocked, not terminal


def test_update_plan_endpoint_passes_ack_param(monkeypatch):
    store = _store()
    store.create_release(ReleaseManifest(
        version="2026.07.19", git_sha="a", modules=dict(_MODULES),
        rollback_kind="restore_required"))
    store.record_backup(BackupRun("bak_plan_ack", "dep_a", "success"))  # B6
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    blocked = operator_router.update_plan("dep_a", "2026.07.19", principal=_admin())
    assert blocked.allowed is False
    assert blocked.reason == "restore_required_ack_needed"
    assert blocked.rollback_kind == "restore_required"

    acked = operator_router.update_plan(
        "dep_a", "2026.07.19", ack_restore_required=True, principal=_admin())
    assert acked.allowed is True


# --- P4-09: promotion-time migration-linter wiring ----------------------------

_DROP_DELTA = {"alembic": [["0021_drop.py", "def upgrade():\n    op.drop_column('t', 'c')\n"]]}
_ADD_DELTA = {"alembic": [["0022_add.py", "def upgrade():\n    op.add_column('t', sa.Column('c', sa.String()))\n"]]}


def test_create_release_stamps_classified_rollback_kind(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    # A DROP COLUMN with NO declared rollback_kind -> stamped restore_required.
    out = operator_router.create_release(operator_router.ReleaseCreate(
        version="2026.09.1", git_sha="abc", modules=dict(_MODULES), migration_delta=_DROP_DELTA,
    ), principal=_admin())

    assert out.rollback_kind == "restore_required"
    assert store.get_release("2026.09.1").rollback_kind == "restore_required"


def test_create_release_rejects_understated_rollback_kind(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    # linter says restore_required, operator declares code_only (no override) -> 400 + findings.
    with pytest.raises(HTTPException) as ei:
        operator_router.create_release(operator_router.ReleaseCreate(
            version="v_bad", git_sha="a", modules=dict(_MODULES),
            rollback_kind="code_only", migration_delta=_DROP_DELTA,
        ), principal=_admin())
    assert ei.value.status_code == 400
    assert "disagrees with migration classification" in ei.value.detail
    assert store.get_release("v_bad") is None       # never persisted

    # override=True -> STILL 400: you cannot override to a LOOSER kind (linter is the floor).
    with pytest.raises(HTTPException) as ei2:
        operator_router.create_release(operator_router.ReleaseCreate(
            version="v_bad2", git_sha="a", modules=dict(_MODULES),
            rollback_kind="code_only", rollback_kind_override=True, migration_delta=_DROP_DELTA,
        ), principal=_admin())
    assert ei2.value.status_code == 400
    assert store.get_release("v_bad2") is None

    # an operator-STRICTER value (restore_required on a code_only add-column delta) -> accepted.
    out = operator_router.create_release(operator_router.ReleaseCreate(
        version="v_strict", git_sha="a", modules=dict(_MODULES),
        rollback_kind="restore_required", migration_delta=_ADD_DELTA,
    ), principal=_admin())
    assert out.rollback_kind == "restore_required"


def test_create_release_no_delta_is_inert(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    # No migration_delta -> no classification; rollback_kind taken verbatim (Phase-3 behavior).
    out = operator_router.create_release(operator_router.ReleaseCreate(
        version="v_inert", git_sha="a", modules=dict(_MODULES), rollback_kind="code_only",
    ), principal=_admin())
    assert out.rollback_kind == "code_only"
    assert store.get_release("v_inert").rollback_kind == "code_only"


def test_create_release_add_column_delta_classifies_code_only(monkeypatch):
    store = _store()
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)

    # A purely-additive delta with no declared kind -> stamped code_only.
    out = operator_router.create_release(operator_router.ReleaseCreate(
        version="v_add", git_sha="a", modules=dict(_MODULES), migration_delta=_ADD_DELTA,
    ), principal=_admin())
    assert out.rollback_kind == "code_only"

"""Operator control plane: releases, customer deployments, backups and rollouts."""

from __future__ import annotations

import pytest

from app.controlplane.base import (
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    ReleaseManifest,
    RolloutRun,
)
from app.controlplane.memory import MemoryControlPlaneStore


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

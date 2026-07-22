"""Customer teardown review is intentionally record-only and non-destructive."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import app.routers.operator as operator_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import (
    CustomerDeployment,
    TEARDOWN_EXECUTION_DISABLED_RESULT,
    TEARDOWN_REQUEST_APPROVED,
    TEARDOWN_REQUEST_EXECUTION_DISABLED,
    TEARDOWN_REQUEST_EXPIRED,
    TEARDOWN_REQUEST_PENDING,
)
from app.controlplane.memory import MemoryControlPlaneStore
from app.fleet.base import FleetKey
from app.fleet.memory import MemoryFleetStore
from app.platform.base import Account, LegalHold
from app.platform.memory import MemoryPlatformStore
from app.provisioning.hetzner.broker import BrokerDestroyResult, InProcessHetznerBroker


def _admin(user_id: str) -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id=user_id,
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
        principal_type="human",
    )


def _settings():
    return SimpleNamespace(is_operator_surface=True, operator_mode=True)


def _protocol_stores(monkeypatch, *, runs=None, fleet_keys=()):
    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id="dep_acme",
        customer_name="Acme",
        account_id="acme",
        deployment_type="dedicated_server",
        release_ring="pilot",
    ))
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id="acme",
        kind="organization",
        name="Acme",
        owner_user_id="requester@example.test",
    ))
    fleet = MemoryFleetStore()
    for key in fleet_keys:
        fleet.create_key(key)
    run_store = _FakeRunStore(list(runs or ()))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: run_store)
    monkeypatch.setattr(operator_router, "get_settings", _settings)
    return control, platform


class _FakeRunStore:
    """Minimal provisioning-run store: the manifest resolver only calls
    list_runs(deployment_id=...), so runs are plain SimpleNamespaces."""

    def __init__(self, runs):
        self._runs = list(runs)

    def list_runs(self, account_id: str = "", deployment_id: str = ""):
        return [r for r in self._runs if r.deployment_id == deployment_id]


def _run(run_id, manifest, *, deployment_id="dep_acme", created_at="2026-07-20T00:00:00+00:00"):
    return SimpleNamespace(
        id=run_id,
        deployment_id=deployment_id,
        created_at=created_at,
        result_payload={"erasure_manifest": manifest},
    )


class _FakeBroker:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def destroy_box(self, deployment_id, *, confirm):
        self.calls.append((deployment_id, confirm))
        if self._error is not None:
            raise self._error
        return self._result


def _patch_broker(monkeypatch, broker):
    monkeypatch.setattr(
        "app.provisioning.hetzner.broker.build_hetzner_broker", lambda settings: broker,
    )


def _reach_approved(created):
    _approve(created, principal=_admin("approver-one@example.test"))
    return _approve(created, principal=_admin("approver-two@example.test"))


def _execute(created, *, principal=None, phrase="decommission dep_acme"):
    return operator_router.execute_customer_teardown_request(
        "dep_acme",
        created.request.id,
        operator_router.CustomerTeardownExecute(confirmation_phrase=phrase),
        principal=principal or _admin("approver-one@example.test"),
    )


def _open_request(monkeypatch, requester: str = "requester@example.test", **kwargs):
    control, platform = _protocol_stores(monkeypatch, **kwargs)
    created = operator_router.create_customer_teardown_request(
        "dep_acme",
        operator_router.CustomerTeardownRequestCreate(
            legal_hold_evidence_ref="legal-review-2026-07-17",
            backup_retention_evidence_ref="backup-retention-2026-07-17",
        ),
        principal=_admin(requester),
    )
    return control, platform, created


def _approve(created, *, principal: Principal, nonce: str | None = None):
    return operator_router.approve_customer_teardown_request(
        "dep_acme",
        created.request.id,
        operator_router.CustomerTeardownApproval(
            nonce=created.approval_nonce if nonce is None else nonce,
        ),
        principal=principal,
    )


def _audit_actions(platform: MemoryPlatformStore) -> list[tuple[str, str]]:
    return [(event.action, event.decision) for event in platform.list_audit("acme")]


def test_teardown_request_rejects_an_active_platform_legal_hold(monkeypatch):
    control, platform = _protocol_stores(monkeypatch)
    platform.create_legal_hold(LegalHold(
        id="hold_acme",
        account_id="acme",
        subject_ref="litigation-42",
        reason="Preserve account data",
        created_by="legal@example.test",
    ))

    with pytest.raises(HTTPException) as exc:
        operator_router.create_customer_teardown_request(
            "dep_acme",
            operator_router.CustomerTeardownRequestCreate(
                legal_hold_evidence_ref="legal-review-2026-07-17",
                backup_retention_evidence_ref="backup-retention-2026-07-17",
            ),
            principal=_admin("requester@example.test"),
        )

    assert exc.value.status_code == 409
    assert control.list_teardown_requests("dep_acme") == []
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.request_denied",
        "denied_legal_hold",
    )


def test_teardown_approval_rechecks_active_legal_holds(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    platform.create_legal_hold(LegalHold(
        id="hold_after_request",
        account_id="acme",
        subject_ref="litigation-43",
        reason="Hold opened during review",
        created_by="legal@example.test",
    ))

    with pytest.raises(HTTPException) as exc:
        _approve(created, principal=_admin("approver-one@example.test"))

    assert exc.value.status_code == 409
    assert control.get_teardown_request(created.request.id).status == TEARDOWN_REQUEST_PENDING
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.approval_denied",
        "denied_legal_hold",
    )


def test_teardown_approval_rejects_missing_and_wrong_nonces_without_recording_approval(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    approver = _admin("approver-one@example.test")

    with pytest.raises(HTTPException) as missing:
        _approve(created, principal=approver, nonce="")
    with pytest.raises(HTTPException) as wrong:
        _approve(created, principal=approver, nonce="not-the-approval-nonce")

    stored = control.get_teardown_request(created.request.id)
    assert missing.value.status_code == 400
    assert wrong.value.status_code == 400
    assert stored.approver_ids == ()
    assert stored.status == TEARDOWN_REQUEST_PENDING
    assert stored.nonce_hash != created.approval_nonce
    assert created.approval_nonce not in repr(stored)
    assert not hasattr(created.request, "nonce_hash")
    assert all(created.approval_nonce not in str(event.meta) for event in platform.list_audit("acme"))
    assert _audit_actions(platform)[-2:] == [
        ("customer_teardown.approval_denied", "denied"),
        ("customer_teardown.approval_denied", "denied"),
    ]


def test_expired_teardown_nonce_becomes_a_terminal_non_execution_record(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    request = control.get_teardown_request(created.request.id)
    control._teardown_requests[request.id] = replace(
        request,
        nonce_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )

    with pytest.raises(HTTPException) as exc:
        _approve(created, principal=_admin("approver-one@example.test"))

    stored = control.get_teardown_request(created.request.id)
    assert exc.value.status_code == 409
    assert stored.status == TEARDOWN_REQUEST_EXPIRED
    assert stored.execution_result == TEARDOWN_EXECUTION_DISABLED_RESULT
    assert stored.completed_at
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.approval_denied",
        "denied_expired",
    )


def test_requester_and_duplicate_approvals_are_rejected(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    requester = _admin("requester@example.test")
    approver = _admin("approver-one@example.test")

    with pytest.raises(HTTPException) as requester_attempt:
        _approve(created, principal=requester)
    first = _approve(created, principal=approver)
    with pytest.raises(HTTPException) as duplicate_attempt:
        _approve(created, principal=approver)

    stored = control.get_teardown_request(created.request.id)
    assert requester_attempt.value.status_code == 409
    assert duplicate_attempt.value.status_code == 409
    assert first.status == TEARDOWN_REQUEST_PENDING
    assert stored.approver_ids == ("approver-one@example.test",)
    assert stored.status == TEARDOWN_REQUEST_PENDING
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.approval_denied",
        "denied",
    )


def test_two_independent_approvals_reach_approved_without_destroying(monkeypatch):
    control, platform, created = _open_request(monkeypatch)

    # Approval never touches infrastructure — only the execute endpoint does.
    with patch.object(InProcessHetznerBroker, "destroy_box", autospec=True) as destroy_box:
        first = _approve(created, principal=_admin("approver-one@example.test"))
        terminal = _approve(created, principal=_admin("approver-two@example.test"))

    stored = control.get_teardown_request(created.request.id)
    destroy_box.assert_not_called()
    assert first.status == TEARDOWN_REQUEST_PENDING
    assert terminal.status == TEARDOWN_REQUEST_APPROVED
    assert terminal.execution_result == ""   # APPROVED is executable, not completed
    assert not terminal.completed_at
    assert stored.approver_ids == (
        "approver-one@example.test",
        "approver-two@example.test",
    )
    assert _audit_actions(platform)[-2:] == [
        ("customer_teardown.approval_recorded", "recorded"),
        ("customer_teardown.approved", "approved"),
    ]


def test_teardown_postgres_mapper_and_migration_are_additive_record_only():
    from app.controlplane.postgres import PostgresControlPlaneStore

    store = object.__new__(PostgresControlPlaneStore)
    at = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    request = store._teardown_request((
        "tear_1", "dep_acme", "acme", "a" * 64, at,
        "legal-ref", "backup-ref", "requester@example.test",
        ["approver-one@example.test", "approver-two@example.test"],
        TEARDOWN_REQUEST_EXECUTION_DISABLED,
        TEARDOWN_EXECUTION_DISABLED_RESULT,
        at, at, at,
    ))

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0028_customer_teardown_protocol.py"
    )
    spec = importlib.util.spec_from_file_location("teardown_protocol_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    source = migration_path.read_text()

    assert len(PostgresControlPlaneStore._TEARDOWN_REQUEST_COLS.split(",")) == 14
    assert request.approver_ids == (
        "approver-one@example.test",
        "approver-two@example.test",
    )
    assert request.nonce_hash == "a" * 64
    assert request.execution_result == TEARDOWN_EXECUTION_DISABLED_RESULT
    assert migration.revision == "0028_customer_teardown_protocol"
    assert migration.down_revision == "0027_ai_agent_run_leases"
    assert "control_customer_teardown_requests" in source
    assert "ON DELETE RESTRICT" in source
    assert "execution_disabled: no customer resources were deleted" in source


# --- executor lifecycle (PR3: real Hetzner teardown on top of the record) ---

_FULL_MANIFEST = {
    "server_id": "srv-1",
    "volume_ids": ["vol-1"],
    "dns_record_id": "dns-1",
    "firewall_id": "fw-1",
}


def test_resolve_erasure_manifest_accumulates_across_reuse_runs(monkeypatch):
    # The LATEST run is an idempotent reuse (server id only); the ORIGINAL creating
    # run holds volume/DNS/firewall. Latest-only would leak them — accumulation must not.
    creating = _run("run-old", _FULL_MANIFEST, created_at="2026-07-10T00:00:00+00:00")
    reuse = _run(
        "run-new",
        {"server_id": "srv-1", "volume_ids": [], "dns_record_id": "", "firewall_id": ""},
        created_at="2026-07-20T00:00:00+00:00",
    )
    monkeypatch.setattr(
        operator_router, "get_provisioning_run_store", lambda: _FakeRunStore([reuse, creating])
    )
    merged = operator_router._resolve_erasure_manifest("dep_acme")
    assert merged == _FULL_MANIFEST
    assert operator_router._manifest_has_resources(merged)
    assert not operator_router._manifest_has_resources(
        {"server_id": "", "volume_ids": [], "dns_record_id": "", "firewall_id": ""}
    )


def test_relaxed_dual_control_lets_one_identity_reach_approved(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    # Sole-operator relaxation: min_approvals=1 + self-approval. base.py reads the
    # REAL app.config.get_settings (not the operator router's), so patch it there.
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(teardown_min_approvals=1, teardown_allow_self_approval=True),
    )
    out = _approve(created, principal=_admin("requester@example.test"))  # requester self-approves
    assert out.status == TEARDOWN_REQUEST_APPROVED
    assert out.approver_ids == ["requester@example.test"]   # OUT model returns a list


def test_teardown_min_approvals_is_bounded_at_settings_load():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings(teardown_min_approvals=0)
    assert "teardown_min_approvals" in str(exc.value)


def test_execute_destroys_infrastructure_revokes_keys_and_tombstones(monkeypatch):
    control, platform, created = _open_request(
        monkeypatch,
        runs=[_run("run-1", _FULL_MANIFEST)],
        fleet_keys=[FleetKey(id="key1", key_hash="h", deployment_id="dep_acme")],
    )
    _reach_approved(created)
    broker = _FakeBroker(result=BrokerDestroyResult(
        deployment_id="dep_acme",
        servers_deleted=("srv-1",),
        volumes_deleted=("vol-1",),
        firewalls_deleted=("fw-1",),
        dns_deleted=("dep-acme/A",),
        nothing_found=False,
    ))
    _patch_broker(monkeypatch, broker)

    out = _execute(created)

    assert broker.calls == [("dep_acme", True)]
    assert out.record_only is False
    assert out.servers_deleted == ["srv-1"]
    assert out.dns_deleted == ["dep-acme/A"]
    assert out.fleet_keys_revoked == 1
    stored = control.get_teardown_request(created.request.id)
    assert stored.status == "executed"
    assert stored.completed_at
    # Tombstoned: gone from the fleet listing, still retrievable for audit.
    assert control.list_deployments() == []
    assert control.get_deployment("dep_acme").removed_at
    assert operator_router.get_fleet_store().get_key("key1").status == "revoked"
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.executed",
        "infrastructure_destroyed",
    )


def test_execute_record_only_when_no_infrastructure_remains(monkeypatch):
    control, platform, created = _open_request(monkeypatch)  # no runs, no fleet keys
    _reach_approved(created)
    broker = _FakeBroker(result=BrokerDestroyResult(deployment_id="dep_acme", nothing_found=True))
    _patch_broker(monkeypatch, broker)

    out = _execute(created)

    assert broker.calls == [("dep_acme", True)]   # broker still VERIFIES nothing remained
    assert out.record_only is True
    assert "No infrastructure" in out.warning
    stored = control.get_teardown_request(created.request.id)
    assert stored.status == "executed"
    assert control.list_deployments() == []
    assert _audit_actions(platform)[-1] == ("customer_teardown.executed", "record_only")


def test_execute_requires_the_exact_confirmation_phrase(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    _reach_approved(created)
    broker = _FakeBroker(result=BrokerDestroyResult(deployment_id="dep_acme", nothing_found=True))
    _patch_broker(monkeypatch, broker)

    with pytest.raises(HTTPException) as exc:
        _execute(created, phrase="decommission wrong-box")

    assert exc.value.status_code == 400
    assert broker.calls == []                       # never reached the broker
    stored = control.get_teardown_request(created.request.id)
    assert stored.status == TEARDOWN_REQUEST_APPROVED   # unchanged, still executable
    assert control.list_deployments() != []             # not tombstoned
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.execution_denied",
        "denied_phrase_mismatch",
    )


def test_execute_refuses_a_request_that_is_not_approved(monkeypatch):
    control, platform, created = _open_request(monkeypatch)  # still pending
    broker = _FakeBroker(result=BrokerDestroyResult(deployment_id="dep_acme", nothing_found=True))
    _patch_broker(monkeypatch, broker)

    with pytest.raises(HTTPException) as exc:
        _execute(created)

    assert exc.value.status_code == 409
    assert broker.calls == []
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.execution_denied",
        "denied_not_approved",
    )


def test_execute_is_operator_mode_only(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    _reach_approved(created)
    monkeypatch.setattr(
        operator_router,
        "get_settings",
        lambda: SimpleNamespace(is_operator_surface=True, operator_mode=False),
    )
    with pytest.raises(HTTPException) as exc:
        _execute(created)
    assert exc.value.status_code == 404


def test_execute_rechecks_legal_hold_at_execute_time(monkeypatch):
    control, platform, created = _open_request(monkeypatch)
    _reach_approved(created)
    platform.create_legal_hold(LegalHold(
        id="hold_at_execute",
        account_id="acme",
        subject_ref="litigation-99",
        reason="Hold opened after approval",
        created_by="legal@example.test",
    ))
    broker = _FakeBroker(result=BrokerDestroyResult(deployment_id="dep_acme", nothing_found=True))
    _patch_broker(monkeypatch, broker)

    with pytest.raises(HTTPException) as exc:
        _execute(created)

    assert exc.value.status_code == 409
    assert broker.calls == []
    assert control.list_deployments() != []
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.execution_denied",
        "denied_legal_hold",
    )


def test_execute_fails_closed_when_broker_unavailable_with_recorded_infra(monkeypatch):
    control, platform, created = _open_request(monkeypatch, runs=[_run("run-1", _FULL_MANIFEST)])
    _reach_approved(created)
    broker = _FakeBroker(error=RuntimeError("remote Hetzner broker is unavailable"))
    _patch_broker(monkeypatch, broker)

    with pytest.raises(HTTPException) as exc:
        _execute(created)

    assert exc.value.status_code == 502
    stored = control.get_teardown_request(created.request.id)
    assert stored.status == "execution_failed"
    assert control.list_deployments() != []   # real infra never tombstoned without a destroy
    assert _audit_actions(platform)[-1] == (
        "customer_teardown.execution_failed",
        "broker_unavailable",
    )

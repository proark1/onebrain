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
    TEARDOWN_REQUEST_EXECUTION_DISABLED,
    TEARDOWN_REQUEST_EXPIRED,
    TEARDOWN_REQUEST_PENDING,
)
from app.controlplane.memory import MemoryControlPlaneStore
from app.platform.base import Account, LegalHold
from app.platform.memory import MemoryPlatformStore
from app.provisioning.hetzner.broker import InProcessHetznerBroker


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


def _protocol_stores(monkeypatch):
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
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_settings", _settings)
    return control, platform


def _open_request(monkeypatch, requester: str = "requester@example.test"):
    control, platform = _protocol_stores(monkeypatch)
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


def test_two_independent_approvals_end_with_execution_disabled_and_never_destroy(monkeypatch):
    control, platform, created = _open_request(monkeypatch)

    with patch.object(InProcessHetznerBroker, "destroy_box", autospec=True) as destroy_box:
        first = _approve(created, principal=_admin("approver-one@example.test"))
        terminal = _approve(created, principal=_admin("approver-two@example.test"))

    stored = control.get_teardown_request(created.request.id)
    destroy_box.assert_not_called()
    assert first.status == TEARDOWN_REQUEST_PENDING
    assert terminal.status == TEARDOWN_REQUEST_EXECUTION_DISABLED
    assert terminal.execution_result == TEARDOWN_EXECUTION_DISABLED_RESULT
    assert stored.approver_ids == (
        "approver-one@example.test",
        "approver-two@example.test",
    )
    assert stored.completed_at
    assert _audit_actions(platform)[-2:] == [
        ("customer_teardown.approval_recorded", "recorded"),
        ("customer_teardown.approved_execution_disabled", "execution_disabled"),
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

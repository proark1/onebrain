from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

from app.auth.passwords import hash_password, verify_password
from app.platform.base import Account
from app.platform.memory import MemoryPlatformStore
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore
from app.trust.signing import generate_keypair
from app.user_management.base import USER_MANAGEMENT_CONTRACT, UserManagementJob
from app.user_management.crypto import (
    UserManagementCommand,
    decrypt_result,
    generate_result_keypair,
    result_aad,
    sign_command,
    verify_command,
)
from app.user_management.memory import MemoryUserManagementJobStore, MemoryUserManagementReceiptStore
from app.user_management.service import CustomerUserManagementService, UserManagementError
from app.users.base import User
from app.users.memory import MemoryUserStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _service():
    users = MemoryUserStore()
    users.create(User(
        id="admin-1", email="admin@example.com", display_name="Admin",
        password_hash=hash_password("initial-password"), tenant_id="tenant",
        role_id="admin", location="all",
    ))
    sessions = MemorySessionStore()
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="tenant", kind="organization", name="Tenant", owner_user_id="admin-1"))
    receipts = MemoryUserManagementReceiptStore()
    return CustomerUserManagementService(
        users=users, sessions=sessions, platform=platform, receipts=receipts, tenant_id="tenant",
    ), users, sessions, platform


def _command(action: str, payload: dict):
    private, public = generate_result_keypair()
    now = _now()
    return UserManagementCommand(
        contract=USER_MANAGEMENT_CONTRACT,
        command_id=f"cmd-{action}",
        deployment_id="dep-1",
        action=action,
        idempotency_key=f"idem-{action}",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        result_public_key=public,
        payload=payload,
    ), private


def _decrypt(command, private, envelope):
    return decrypt_result(
        envelope,
        private,
        aad=result_aad(command_id=command.command_id, deployment_id=command.deployment_id, action=command.action),
    )


def test_signed_command_is_domain_and_deployment_bound():
    signing_private, signing_public = generate_keypair()
    command, _ = _command("directory.snapshot", {"include_deleted": False})
    signed = sign_command(command, signing_private)
    assert verify_command(signed, [signing_public], deployment_id="dep-1", now_iso=_now().isoformat())
    assert not verify_command(signed, [signing_public], deployment_id="dep-2", now_iso=_now().isoformat())
    assert not verify_command(replace(signed, action="user.delete"), [signing_public], deployment_id="dep-1", now_iso=_now().isoformat())


def test_create_and_reset_generate_one_time_password_and_replay_same_ciphertext():
    service, users, sessions, platform = _service()
    create, private = _command("user.create", {
        "display_name": "New Person", "email": "New@Example.com",
        "role_id": "front_desk", "location": "munich",
    })
    envelope = service.execute(create)
    result = _decrypt(create, private, envelope)
    assert result["ok"] is True
    password = result["data"]["one_time_password"]
    created = users.get_by_email("new@example.com")
    assert created and created.must_change_password is True
    assert verify_password(password, created.password_hash)
    assert service.execute(create) == envelope

    sessions.create(Session(id="session", user_id=created.id, tenant_id="tenant"))
    reset, reset_private = _command("user.password.reset", {"user_id": created.id})
    reset_result = _decrypt(reset, reset_private, service.execute(reset))["data"]
    assert reset_result["sessions_revoked"] == 1
    assert users.get(created.id).must_change_password is True
    assert verify_password(reset_result["one_time_password"], users.get(created.id).password_hash)
    audit = platform.list_audit("tenant")
    assert audit[-1].meta == {
        "command_id": reset.command_id,
        "deployment_id": reset.deployment_id,
        "error_code": "",
    }
    assert password not in str(audit)


def test_last_admin_and_safe_delete_rules():
    service, users, _, platform = _service()
    disable_admin, _ = _command("user.disable", {"user_id": "admin-1"})
    with pytest.raises(UserManagementError, match="last_active_admin"):
        service.execute(disable_admin)

    users.create(User(
        id="admin-2", email="admin2@example.com", display_name="Admin 2",
        password_hash=hash_password("initial-password"), tenant_id="tenant",
        role_id="admin", location="all",
    ))
    service.execute(disable_admin)
    delete, _ = _command("user.delete", {"user_id": "admin-1"})
    with pytest.raises(UserManagementError, match="ownership_reassignment_required"):
        service.execute(delete)
    platform.upsert_bootstrap_account(Account(id="tenant", kind="organization", name="Tenant", owner_user_id="admin-2"))
    command = replace(delete, command_id="cmd-delete-2", idempotency_key="idem-delete-2")
    service.execute(command)
    deleted = users.get("admin-1")
    assert deleted.status == "deleted"
    assert deleted.display_name == "Deleted user"
    assert deleted.email == "deleted+admin-1@invalid.onebrain"
    assert deleted.role_id == "public"


def test_memory_job_lease_expiry_and_atomic_consumption():
    store = MemoryUserManagementJobStore()
    now = _now()
    job = UserManagementJob(
        id="job", deployment_id="dep", action="user.create", status="queued",
        idempotency_key="idem", requested_by="operator", sealed_payload="sealed",
        sealed_result_private_key="private", result_public_key="public",
        created_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(),
    )
    store.create(job)
    leased = store.lease_next(
        "dep", now_iso=now.isoformat(), lease_expires_at=(now + timedelta(minutes=1)).isoformat(),
    )
    assert leased.status == "leased" and leased.attempts == 1
    completed = store.complete(
        "job", "dep", sender_public_key="sender", nonce="nonce", ciphertext="ciphertext",
        completed_at=now.isoformat(), result_expires_at=(now + timedelta(minutes=10)).isoformat(),
    )
    assert completed.status == "completed"
    assert store.consume_result("job", consumed_at=now.isoformat()).result_ciphertext == "ciphertext"
    assert store.consume_result("job", consumed_at=now.isoformat()) is None


def test_host_agent_rejects_wrong_deployment_and_tampered_command():
    path = Path(__file__).parents[1] / "deploy" / "box" / "onebrain_user_management_agent.py"
    spec = importlib.util.spec_from_file_location("onebrain_user_management_agent", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    signing_private, signing_public = generate_keypair()
    command, _ = _command("directory.snapshot", {"include_deleted": False})
    signed = sign_command(command, signing_private)
    value = signed.__dict__.copy()
    assert module._valid_command(value, "dep-1", signing_public)
    assert not module._valid_command(value, "dep-2", signing_public)
    assert not module._valid_command({**value, "action": "user.delete"}, "dep-1", signing_public)


def test_host_agent_enables_configured_compose_profiles(monkeypatch):
    path = Path(__file__).parents[1] / "deploy" / "box" / "onebrain_user_management_agent.py"
    spec = importlib.util.spec_from_file_location("onebrain_user_management_agent_profiles", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"result":{}}'

    def run(args, **kwargs):
        captured["args"] = args
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setenv("UPDATE_PROFILES", "onebrain assistant ignored communication")
    value = module._run_cli({}, "dep", "keys")

    assert value["ok"] is True
    assert captured["args"][6:12] == [
        "--profile", "onebrain", "--profile", "assistant", "--profile", "communication",
    ]


def test_host_agent_loads_box_then_secret_environment_without_overriding_process(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "deploy" / "box" / "onebrain_user_management_agent.py"
    spec = importlib.util.spec_from_file_location("onebrain_user_management_agent_environment", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    box = tmp_path / "box.env"
    secret = tmp_path / ".env"
    box.write_text(
        "ONEBRAIN_FLEET_KEY=${ONEBRAIN_FLEET_KEY}\n"
        " UPDATE_PROFILES = 'onebrain assistant' \n"
        " HOST_LABEL = 'dev gate' \n"
        "INVALID KEY=ignored\n"
    )
    secret.write_text(' ONEBRAIN_FLEET_KEY = "secret-value" \n')
    monkeypatch.setenv("UPDATE_PROFILES", "process-value")
    monkeypatch.delenv("ONEBRAIN_FLEET_KEY", raising=False)
    monkeypatch.delenv("HOST_LABEL", raising=False)

    module._load_host_environment((box, secret))

    assert module.os.environ["ONEBRAIN_FLEET_KEY"] == "secret-value"
    assert module.os.environ["UPDATE_PROFILES"] == "process-value"
    assert module.os.environ["HOST_LABEL"] == "dev gate"

"""Strict stdin/stdout entry point invoked by the root-owned host agent."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import fields
from datetime import datetime, timezone

from app.auth.principal import HUMAN_TENANT
from app.config import get_settings
from app.deps import (
    get_platform_store,
    get_session_store,
    get_user_management_receipt_store,
    get_user_store,
)
from app.provisioning.customer_bootstrap import decode_customer_bootstrap
from app.user_management.crypto import UserManagementCommand, verify_command
from app.user_management.crypto import encrypt_result, result_aad
from app.user_management.service import (
    CustomerUserManagementService,
    PostgresCustomerUserManagementService,
    UserManagementError,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(value: dict) -> dict:
    if not isinstance(value, dict) or frozenset(value) != {field.name for field in fields(UserManagementCommand)}:
        return {"ok": False, "error_code": "internal_failure"}
    try:
        command = UserManagementCommand(**value)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "internal_failure"}
    settings = get_settings()
    deployment_id = (
        os.environ.get("ONEBRAIN_MANAGEMENT_DEPLOYMENT_ID", "")
        or settings.deployment_id
        or os.environ.get("ONEBRAIN_DEPLOYMENT_ID", "")
    ).strip()
    public_keys = [
        key.strip() for key in (
            os.environ.get("ONEBRAIN_MANAGEMENT_PUBLIC_KEYS", "")
            or settings.fleet_desired_state_public_keys
            or settings.fleet_desired_state_public_key
            or ""
        ).split(",") if key.strip()
    ]
    if not verify_command(command, public_keys, deployment_id=deployment_id, now_iso=_now()):
        return {"ok": False, "error_code": "command_expired" if command.expires_at <= _now() else "internal_failure"}
    bootstrap = decode_customer_bootstrap(settings.customer_bootstrap)
    tenant_id = bootstrap.account_id if bootstrap else HUMAN_TENANT
    service = (
        PostgresCustomerUserManagementService(dsn=settings.pg_database_url, tenant_id=tenant_id)
        if settings.vector_store == "pgvector"
        else CustomerUserManagementService(
            users=get_user_store(),
            sessions=get_session_store(),
            platform=get_platform_store(),
            receipts=get_user_management_receipt_store(),
            tenant_id=tenant_id,
        )
    )
    try:
        return {"ok": True, "result": service.execute(command)}
    except UserManagementError as exc:
        encrypted = encrypt_result(
            {"ok": False, "error_code": exc.code, "details": exc.details},
            command.result_public_key,
            aad=result_aad(
                command_id=command.command_id,
                deployment_id=command.deployment_id,
                action=command.action,
            ),
        )
        return {"ok": True, "result": encrypted}
    except Exception:
        return {"ok": False, "error_code": "internal_failure"}


def main() -> int:
    try:
        value = json.load(sys.stdin)
    except Exception:
        value = None
    sys.stdout.write(json.dumps(run(value), separators=(",", ":"), sort_keys=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

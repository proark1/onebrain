"""Authorization helpers for human-facing AI Employees operations."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from app.ai_employees.contracts import AI_EMPLOYEES_APP_ID
from app.auth.account_access import (
    authorize_account_admin,
    authorize_account_member,
    is_account_admin,
)


@dataclass(frozen=True)
class AiEmployeesAccess:
    account: object
    space: object
    installation: object
    is_admin: bool

    @property
    def active(self) -> bool:
        return self.installation.status == "active"

    def allows(self, purpose: str) -> bool:
        return self.active and purpose in self.installation.allowed_purposes


def find_ai_employee_installation(account_id: str, space_id: str, platform_store):
    matches = [
        installation
        for installation in platform_store.list_app_installations(account_id)
        if installation.app_id == AI_EMPLOYEES_APP_ID
        and space_id in installation.enabled_space_ids
        and installation.status in {"active", "paused"}
    ]
    return sorted(matches, key=lambda row: (row.status != "active", row.id))[0] if matches else None


def authorize_ai_employee_reader(
    principal,
    account_id: str,
    space_id: str,
    platform_store,
) -> AiEmployeesAccess:
    account = authorize_account_member(principal, account_id, space_id, platform_store)
    space = platform_store.get_space((space_id or "").strip())
    if not space or space.account_id != account.id or space.status != "active":
        raise HTTPException(status_code=404, detail="Space not found.")
    installation = find_ai_employee_installation(account.id, space.id, platform_store)
    if not installation or "ai_employee_read" not in installation.allowed_purposes:
        raise HTTPException(status_code=403, detail="AI Employees is not enabled for this workspace.")
    return AiEmployeesAccess(
        account=account,
        space=space,
        installation=installation,
        is_admin=is_account_admin(principal, account, platform_store),
    )


def authorize_ai_employee_purpose(
    principal,
    account_id: str,
    space_id: str,
    purpose: str,
    platform_store,
    *,
    admin_required: bool = False,
) -> AiEmployeesAccess:
    access = authorize_ai_employee_reader(
        principal, account_id, space_id, platform_store,
    )
    if admin_required:
        if principal.tenant_id != account_id:
            raise HTTPException(status_code=404, detail="Account not found.")
        authorize_account_admin(principal, account_id, platform_store)
    if not access.active or purpose not in access.installation.allowed_purposes:
        raise HTTPException(
            status_code=403,
            detail="AI Employees is paused or that capability is not enabled for this workspace.",
        )
    return access

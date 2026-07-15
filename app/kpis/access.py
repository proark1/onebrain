"""KPI-specific human authorization helpers."""

from __future__ import annotations

from fastapi import HTTPException

from app.auth.account_access import authorize_account_admin, authorize_account_member
from app.kpis.base import (
    KPI_APP_ID,
    KPI_CONFIGURE_PURPOSE,
    KPI_READ_PURPOSE,
    KPI_SNAPSHOT_WRITE_PURPOSE,
)


def authorize_kpi_reader(principal, account_id: str, space_id: str, platform_store):
    account = authorize_account_member(principal, account_id, space_id, platform_store)
    space = _require_space(account.id, space_id, platform_store)
    _require_app_access(account.id, space.id, KPI_READ_PURPOSE, platform_store)
    return account, space


def authorize_kpi_configurer(principal, account_id: str, space_id: str, platform_store):
    if principal.tenant_id != account_id:
        raise HTTPException(status_code=404, detail="Account not found.")
    account = authorize_account_admin(principal, account_id, platform_store)
    space = _require_space(account.id, space_id, platform_store)
    _require_app_access(account.id, space.id, KPI_CONFIGURE_PURPOSE, platform_store)
    return account, space


def authorize_kpi_manual_writer(principal, account_id: str, space_id: str, platform_store):
    if principal.tenant_id != account_id:
        raise HTTPException(status_code=404, detail="Account not found.")
    account = authorize_account_admin(principal, account_id, platform_store)
    space = _require_space(account.id, space_id, platform_store)
    _require_app_access(account.id, space.id, KPI_SNAPSHOT_WRITE_PURPOSE, platform_store)
    return account, space


def _require_space(account_id: str, space_id: str, platform_store):
    space = platform_store.get_space((space_id or "").strip())
    if not space or space.account_id != account_id or space.status != "active":
        raise HTTPException(status_code=404, detail="Space not found.")
    return space


def _require_app_access(account_id: str, space_id: str, purpose: str, platform_store) -> None:
    decision = platform_store.check_app_access(account_id, KPI_APP_ID, space_id, purpose)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="KPI Dashboard is not enabled for this workspace.")

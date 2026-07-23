"""Accounting-specific human authorization helpers.

Mirrors ``app/kpis/access.py``: a workspace is readable only when the caller is an
account member of an active space that has the ``buchhaltung`` app installed with
the ``accounting_read`` purpose. Any gap fails closed (404 space / 403 not enabled).
"""

from __future__ import annotations

from fastapi import HTTPException

from app.accounting.base import (
    ACCOUNTING_APP_ID,
    ACCOUNTING_CONFIGURE_PURPOSE,
    ACCOUNTING_READ_PURPOSE,
    accounting_category_id,
)
from app.auth.account_access import authorize_account_member, is_account_admin


def authorize_accounting_reader(principal, account_id: str, space_id: str, platform_store):
    account = authorize_account_member(principal, account_id, space_id, platform_store)
    space = _require_space(account.id, space_id, platform_store)
    _require_app_access(account.id, space.id, ACCOUNTING_READ_PURPOSE, platform_store)
    _require_category_member(principal, account, space.id, platform_store)
    return account, space


def authorize_accounting_writer(principal, account_id: str, space_id: str, platform_store):
    """Confirming/correcting a booking is a routine finance action — a workspace
    member with the ``accounting_configure`` purpose, not necessarily an admin."""
    account = authorize_account_member(principal, account_id, space_id, platform_store)
    space = _require_space(account.id, space_id, platform_store)
    _require_app_access(account.id, space.id, ACCOUNTING_CONFIGURE_PURPOSE, platform_store)
    _require_category_member(principal, account, space.id, platform_store)
    return account, space


def _require_category_member(principal, account, space_id: str, platform_store) -> None:
    """Invoices live in the confidential ``buchhaltung`` Drive category (plan §7): only
    its members — plus account admins — may see the structured content. The app purpose
    alone is not enough, or any workspace member could read invoice bodies."""
    if is_account_admin(principal, account, platform_store):
        return
    group_id = accounting_category_id(space_id)
    memberships = platform_store.list_access_group_memberships(account.id, principal.user_id)
    if any(m.group_id == group_id and m.status == "active" for m in memberships):
        return
    raise HTTPException(status_code=403, detail="Accounting is restricted to Buchhaltung members.")


def _require_space(account_id: str, space_id: str, platform_store):
    space = platform_store.get_space((space_id or "").strip())
    if not space or space.account_id != account_id or space.status != "active":
        raise HTTPException(status_code=404, detail="Space not found.")
    return space


def _require_app_access(account_id: str, space_id: str, purpose: str, platform_store) -> None:
    decision = platform_store.check_app_access(account_id, ACCOUNTING_APP_ID, space_id, purpose)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="Accounting is not enabled for this workspace.")

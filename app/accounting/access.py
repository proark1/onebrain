"""Accounting-specific human authorization helpers.

Mirrors ``app/kpis/access.py``: a workspace is readable only when the caller is an
account member of an active space that has the ``buchhaltung`` app installed with
the ``accounting_read`` purpose. Any gap fails closed (404 space / 403 not enabled).
"""

from __future__ import annotations

from fastapi import HTTPException

from app.accounting.base import ACCOUNTING_APP_ID, ACCOUNTING_READ_PURPOSE
from app.auth.account_access import authorize_account_member


def authorize_accounting_reader(principal, account_id: str, space_id: str, platform_store):
    account = authorize_account_member(principal, account_id, space_id, platform_store)
    space = _require_space(account.id, space_id, platform_store)
    _require_app_access(account.id, space.id, ACCOUNTING_READ_PURPOSE, platform_store)
    return account, space


def _require_space(account_id: str, space_id: str, platform_store):
    space = platform_store.get_space((space_id or "").strip())
    if not space or space.account_id != account_id or space.status != "active":
        raise HTTPException(status_code=404, detail="Space not found.")
    return space


def _require_app_access(account_id: str, space_id: str, purpose: str, platform_store) -> None:
    decision = platform_store.check_app_access(account_id, ACCOUNTING_APP_ID, space_id, purpose)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="Accounting is not enabled for this workspace.")

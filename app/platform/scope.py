"""Helpers for human-facing account/space scoping."""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException

from app.auth.principal import Principal


def selected_space_id(principal: Principal) -> str:
    if principal.space_ids and len(principal.space_ids) == 1:
        return next(iter(principal.space_ids))
    return ""


def scoped_human_principal(account_id: str, space_id: str, principal: Principal, platform_store) -> Principal:
    account_id = (account_id or "").strip()
    space_id = (space_id or "").strip()
    if not account_id and not space_id:
        return principal
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can use platform-scoped workspace operations.")
    if not account_id or not space_id:
        raise HTTPException(status_code=400, detail="account_id and space_id must be provided together.")
    if account_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="This user is not pinned to that account.")
    space = platform_store.get_space(space_id)
    if not space or space.account_id != account_id:
        raise HTTPException(status_code=404, detail="Space not found for this account.")
    return replace(principal, account_id=account_id, space_ids=frozenset({space_id}))


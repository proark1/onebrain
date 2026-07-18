"""Member-scoped Drive roots and direct-file authorization."""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException

from app.auth.account_access import authorize_account_member, is_account_member
from app.auth.principal import Principal
from app.drive.base import DriveFile, DriveFolder, DriveRoot
from app.platform.base import PRIVATE_SPACE_KINDS, Space
from app.security.policy import STATUS_APPROVED, Classification


def resolve_space_context(account_id: str, space_id: str, platform_store) -> tuple[Space, str]:
    account_id = (account_id or "").strip()
    space_id = (space_id or "").strip()
    space = platform_store.get_space(space_id)
    if not space or space.account_id != account_id or space.status != "active":
        raise HTTPException(status_code=404, detail="Drive space not found.")
    if space.kind not in PRIVATE_SPACE_KINDS:
        return space, ""
    candidates = {
        row.user_id for row in platform_store.list_memberships(account_id)
        if row.space_id == space_id and row.status == "active" and row.user_id
    }
    account = platform_store.get_account(account_id)
    if not candidates and account and account.owner_user_id:
        candidates.add(account.owner_user_id)
    if len(candidates) != 1:
        raise HTTPException(status_code=409, detail="Private Drive ownership is unresolved.")
    return space, next(iter(candidates))


def authorize_drive_space(
    principal: Principal, account_id: str, space_id: str, platform_store,
) -> tuple[Space, str]:
    authorize_account_member(principal, account_id, space_id, platform_store)
    space, owner_user_id = resolve_space_context(account_id, space_id, platform_store)
    if owner_user_id and owner_user_id != principal.user_id:
        raise HTTPException(status_code=404, detail="Drive space not found.")
    return space, owner_user_id


def list_drive_roots(principal: Principal, platform_store) -> list[DriveRoot]:
    if principal.principal_type != "human":
        return []
    account = platform_store.get_account(principal.tenant_id)
    if not account:
        return []
    roots: list[DriveRoot] = []
    for space in platform_store.list_spaces(account.id):
        if space.status != "active" or not is_account_member(principal, account, space.id, platform_store):
            continue
        try:
            _, owner_user_id = resolve_space_context(account.id, space.id, platform_store)
        except HTTPException:
            continue
        if owner_user_id and owner_user_id != principal.user_id:
            continue
        kind = "personal" if owner_user_id else "space"
        roots.append(DriveRoot(
            id=space.id,
            account_id=account.id,
            space_id=space.id,
            kind=kind,
            name="My Drive" if kind == "personal" and space.kind == "personal" else space.name,
            owner_user_id=owner_user_id,
        ))
    return sorted(roots, key=lambda row: (row.kind != "personal", row.name.casefold(), row.id))


def file_access_probe(file: DriveFile) -> dict:
    classification = Classification.parse(file.classification)
    return {
        "tenant_id": file.tenant_id,
        "account_id": file.account_id,
        "space_id": file.space_id,
        "space_kind": file.space_kind,
        "owner_user_id": file.owner_user_id,
        "classification": int(classification),
        "classification_label": classification.name.lower(),
        "location": file.location,
        "category": file.category,
        # Approval is an AI publication state. Authorized humans must still be
        # able to list/download/review a pending original, so direct-file access
        # evaluates the audience as if its publication status were approved.
        "status": STATUS_APPROVED,
    }


def folder_access_probe(
    folder: DriveFolder, *, space_kind: str = "", owner_user_id: str = "",
) -> dict:
    classification = Classification.parse(folder.default_classification)
    return {
        "tenant_id": folder.tenant_id,
        "account_id": folder.account_id,
        "space_id": folder.space_id,
        "space_kind": space_kind,
        "owner_user_id": owner_user_id,
        "classification": int(classification),
        "classification_label": classification.name.lower(),
        "location": folder.default_location,
        "category": folder.default_category,
        "status": STATUS_APPROVED,
    }


def can_access_file(principal: Principal, file: DriveFile) -> bool:
    scoped = replace(
        principal,
        account_id=file.account_id,
        space_ids=frozenset({file.space_id}),
    )
    return scoped.access_filter().allows(file_access_probe(file))


def require_file_access(principal: Principal, file: DriveFile) -> None:
    if not can_access_file(principal, file):
        raise HTTPException(status_code=404, detail="Drive file not found.")


def can_access_folder(
    principal: Principal,
    folder: DriveFolder,
    *,
    space_kind: str = "",
    owner_user_id: str = "",
) -> bool:
    scoped = replace(
        principal,
        account_id=folder.account_id,
        space_ids=frozenset({folder.space_id}),
    )
    return scoped.access_filter().allows(folder_access_probe(
        folder, space_kind=space_kind, owner_user_id=owner_user_id,
    ))


def require_folder_access(
    principal: Principal,
    folder: DriveFolder,
    *,
    space_kind: str = "",
    owner_user_id: str = "",
) -> None:
    if not can_access_folder(
        principal, folder, space_kind=space_kind, owner_user_id=owner_user_id,
    ):
        raise HTTPException(status_code=404, detail="Drive folder not found.")

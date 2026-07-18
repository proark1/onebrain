"""Member-scoped HTTP API for the always-on OneBrain Drive feature."""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.principal import Principal, resolve_principal
from app.auth.roles import LOCATIONS
from app.config import get_settings
from app.deps import get_drive_service, get_store
from app.drive import DRIVE_CONTRACT_VERSION
from app.drive.base import DriveConflictError, DriveGenerationConflict, DriveLimitError
from app.security.policy import Classification


router = APIRouter(prefix="/api/drive", tags=["drive"])
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FolderCreateIn(_StrictModel):
    account_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(min_length=1, max_length=128)
    parent_folder_id: str = Field(default="", max_length=128)
    name: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(default="", max_length=128)
    classification: str = Field(default="", max_length=32)
    location: str = Field(default="", max_length=128)
    category: str = Field(default="", max_length=128)
    index_for_ai: bool | None = None


class UploadCreateIn(_StrictModel):
    account_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(min_length=1, max_length=128)
    folder_id: str = Field(default="", max_length=128)
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    index_for_ai: bool | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)
    classification: str = Field(default="", max_length=32)
    location: str = Field(default="", max_length=128)
    category: str = Field(default="", max_length=128)


class UploadCompleteIn(_StrictModel):
    idempotency_key: str = Field(default="", max_length=128)


class ScopedMutationIn(_StrictModel):
    account_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(gt=0)
    idempotency_key: str = Field(default="", max_length=128)


class IndexingIn(ScopedMutationIn):
    enabled: bool


class PermanentDeleteIn(ScopedMutationIn):
    reason: str = Field(default="", max_length=500)


class FolderUpdateIn(ScopedMutationIn):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    classification: str | None = Field(default=None, max_length=32)
    location: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=128)
    index_for_ai: bool | None = None
    confirm_audience_change: bool = False


class FileUpdateIn(FolderUpdateIn):
    folder_id: str | None = Field(default=None, max_length=128)


def _human(principal: Principal = Depends(resolve_principal)) -> Principal:
    if principal.principal_type != "human" or not principal.is_employee:
        raise HTTPException(status_code=403, detail="Drive requires an employee session.")
    return principal


@contextmanager
def _drive_errors():
    try:
        yield
    except HTTPException:
        raise
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'") or "Drive item not found.") from exc
    except (DriveGenerationConflict, DriveConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (DriveLimitError,) as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _root_out(root) -> dict:
    return {
        "id": root.id, "account_id": root.account_id, "space_id": root.space_id,
        "kind": root.kind, "name": root.name,
    }


def _folder_out(folder) -> dict:
    return {
        "kind": "folder",
        "id": folder.id,
        "account_id": folder.account_id,
        "space_id": folder.space_id,
        "name": folder.name,
        "parent_folder_id": folder.parent_id,
        "generation": folder.generation,
        "classification": folder.default_classification,
        "location": folder.default_location,
        "category": folder.default_category,
        "desired_indexed": folder.default_indexed,
        "index_status": "folder",
        "updated_at": folder.updated_at,
        "trashed_at": folder.trashed_at,
    }


def _file_out(file, service=None) -> dict:
    size_bytes = 0
    media_type = "application/octet-stream"
    if service and file.current_revision_id:
        revision = service.store.get_revision(
            file.current_revision_id, account_id=file.account_id, space_id=file.space_id,
        )
        if revision:
            size_bytes, media_type = revision.size_bytes, revision.media_type
    return {
        "kind": "file",
        "id": file.id,
        "account_id": file.account_id,
        "space_id": file.space_id,
        "name": file.name,
        "parent_folder_id": file.folder_id,
        "generation": file.generation,
        "classification": file.classification,
        "location": file.location,
        "category": file.category,
        "desired_indexed": file.desired_indexed,
        "approval_status": file.approval_status,
        "index_status": file.index_status,
        "updated_at": file.updated_at,
        "trashed_at": file.trashed_at,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "download_url": (
            f"/api/drive/files/{file.id}/content?account_id={quote(file.account_id)}&space_id={quote(file.space_id)}"
        ),
    }


def _entries_out(page, service) -> list[dict]:
    entries = [_folder_out(row) for row in page.folders]
    entries.extend(_file_out(row, service) for row in page.files)
    return entries


@router.get("/bootstrap")
def bootstrap(
    account_id: str = "",
    space_id: str = "",
    folder_id: str = "",
    view: str = "files",
    q: str = "",
    principal: Principal = Depends(_human),
):
    service = get_drive_service()
    roots = service.roots(principal)
    selected = next((row for row in roots if row.account_id == account_id and row.space_id == space_id), None)
    selected = selected or (roots[0] if roots else None)
    if not selected:
        return {
            "contract_version": DRIVE_CONTRACT_VERSION,
            "roots": [], "selected_root": None, "breadcrumbs": [], "entries": [],
            "next_cursor": None, "counts": {"review": 0, "trash": 0, "legacy": 0},
            "capabilities": _capabilities(principal),
            "upload": {"max_file_bytes": get_settings().drive_max_file_bytes},
            "audience": {"classifications": [], "locations": [], "departments": []},
        }
    with _drive_errors():
        trashed = view == "trash"
        page = service.list_entries(
            principal,
            account_id=selected.account_id,
            space_id=selected.space_id,
            folder_id=folder_id,
            query=q,
            trashed=trashed,
        )
        breadcrumbs = service.breadcrumbs(
            principal, account_id=selected.account_id, space_id=selected.space_id, folder_id=folder_id,
        ) if folder_id else []
        scoped = replace(principal, account_id=selected.account_id, space_ids=frozenset({selected.space_id}))
        legacy_docs = get_store().list_documents(scoped.access_filter())
        pending = [
            row for row in service.store.list_pending_review(
                account_id=selected.account_id, space_id=selected.space_id,
            ) if service._can_access(principal, row)
        ]
        trash = service.list_entries(
            principal,
            account_id=selected.account_id,
            space_id=selected.space_id,
            trashed=True,
            limit=250,
        )
        entries = _legacy_entries(legacy_docs) if view == "legacy" else _entries_out(page, service)
        if view == "review":
            entries = [_file_out(row, service) for row in pending]
        return {
            "contract_version": DRIVE_CONTRACT_VERSION,
            "roots": [_root_out(row) for row in roots],
            "selected_root": _root_out(selected),
            "breadcrumbs": [_folder_out(row) for row in breadcrumbs],
            "entries": entries,
            "next_cursor": page.next_cursor or None,
            "counts": {
                "review": len(pending),
                "trash": len(trash.folders) + len(trash.files),
                "legacy": len(legacy_docs),
            },
            "capabilities": _capabilities(principal),
            "upload": {"max_file_bytes": get_settings().drive_max_file_bytes},
            "audience": _audience_options(
                principal, service, selected.account_id, selected.space_id,
            ),
        }


@router.get("/items")
def list_items(
    account_id: str,
    space_id: str,
    folder_id: str = "",
    view: str = "files",
    q: str = "",
    cursor: str = "",
    limit: int = 100,
    principal: Principal = Depends(_human),
):
    service = get_drive_service()
    with _drive_errors():
        if view == "legacy":
            service.authorize_space(principal, account_id, space_id)
            scoped = replace(principal, account_id=account_id, space_ids=frozenset({space_id}))
            return {"entries": _legacy_entries(get_store().list_documents(scoped.access_filter())), "next_cursor": None}
        if view == "review":
            service.authorize_space(principal, account_id, space_id)
            rows = [
                row for row in service.store.list_pending_review(account_id=account_id, space_id=space_id)
                if service._can_access(principal, row)
            ]
            return {"entries": [_file_out(row, service) for row in rows], "next_cursor": None}
        page = service.list_entries(
            principal, account_id=account_id, space_id=space_id, folder_id=folder_id,
            query=q, trashed=view == "trash", cursor=cursor, limit=limit,
        )
        breadcrumbs = service.breadcrumbs(
            principal, account_id=account_id, space_id=space_id, folder_id=folder_id,
        ) if folder_id else []
        return {
            "entries": _entries_out(page, service),
            "breadcrumbs": [_folder_out(row) for row in breadcrumbs],
            "next_cursor": page.next_cursor or None,
        }


@router.post("/folders")
def create_folder(body: FolderCreateIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        folder = get_drive_service().create_folder(
            principal,
            account_id=body.account_id,
            space_id=body.space_id,
            parent_id=body.parent_folder_id,
            name=body.name,
            classification=body.classification,
            location=body.location,
            category=body.category,
            index_for_ai=body.index_for_ai,
            idempotency_key=body.idempotency_key,
        )
        return {"folder": _folder_out(folder)}


@router.patch("/folders/{folder_id}")
def update_folder(folder_id: str, body: FolderUpdateIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        folder = get_drive_service().update_folder_defaults(
            principal,
            account_id=body.account_id,
            space_id=body.space_id,
            folder_id=folder_id,
            generation=body.generation,
            name=body.name,
            classification=body.classification,
            location=body.location,
            category=body.category,
            index_for_ai=body.index_for_ai,
            confirm_audience_change=body.confirm_audience_change,
        )
        return {"folder": _folder_out(folder)}


@router.patch("/files/{file_id}")
def update_file(file_id: str, body: FileUpdateIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        file = get_drive_service().update_file(
            principal,
            account_id=body.account_id,
            space_id=body.space_id,
            file_id=file_id,
            generation=body.generation,
            folder_id=body.folder_id,
            name=body.name,
            classification=body.classification,
            location=body.location,
            category=body.category,
            index_for_ai=body.index_for_ai,
            confirm_audience_change=body.confirm_audience_change,
        )
        return {"file": _file_out(file, get_drive_service())}


@router.post("/uploads", status_code=201)
def create_upload(body: UploadCreateIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        upload = get_drive_service().create_upload(
            principal,
            account_id=body.account_id,
            space_id=body.space_id,
            folder_id=body.folder_id,
            name=body.name,
            size_bytes=body.size_bytes,
            index_for_ai=body.index_for_ai,
            idempotency_key=body.idempotency_key,
            classification=body.classification,
            location=body.location,
            category=body.category,
        )
        return {"upload": _upload_out(upload)}


@router.put("/uploads/{upload_id}/content")
async def upload_content(
    upload_id: str,
    request: Request,
    content_type: str = Header(default="", alias="Content-Type"),
    principal: Principal = Depends(_human),
):
    service = get_drive_service()
    writer = None
    with _drive_errors():
        upload, writer = service.begin_upload(principal, upload_id)
        if writer is None:
            return {"upload": _upload_out(upload)}
        try:
            async for chunk in request.stream():
                await run_in_threadpool(writer.write, chunk)
            info = await run_in_threadpool(writer.finish)
            stored = service.finish_upload_content(principal, upload, info, content_type)
            return {"upload": _upload_out(stored)}
        except Exception:
            if writer is not None:
                await run_in_threadpool(writer.abort)
            raise


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, body: UploadCompleteIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        upload, file = get_drive_service().complete_upload(principal, upload_id)
        return {"upload": _upload_out(upload), "file": _file_out(file, get_drive_service())}


@router.get("/files/{file_id}/content")
def download_file(
    file_id: str,
    account_id: str,
    space_id: str,
    range_header: str = Header(default="", alias="Range"),
    principal: Principal = Depends(_human),
):
    service = get_drive_service()
    with _drive_errors():
        file, revision = service.get_revision_for_download(
            principal, account_id=account_id, space_id=space_id, file_id=file_id,
        )
        info = service.blobs.stat(revision.storage_key)
        if not info:
            raise FileNotFoundError("Drive original is unavailable.")
        start, end, status = 0, info.size_bytes - 1, 200
        if range_header:
            match = _RANGE_RE.fullmatch(range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                raise HTTPException(status_code=416, detail="Invalid byte range.")
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:
                suffix = int(match.group(2))
                start = max(0, info.size_bytes - suffix)
            if start >= info.size_bytes or end < start:
                raise HTTPException(
                    status_code=416, detail="Byte range is unsatisfiable.",
                    headers={"Content-Range": f"bytes */{info.size_bytes}"},
                )
            end = min(end, info.size_bytes - 1)
            status = 206
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file.name)}",
            "ETag": f'"{revision.sha256}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        return StreamingResponse(
            service.blobs.iter_range(revision.storage_key, start=start, end=end),
            status_code=status,
            media_type="application/octet-stream",
            headers=headers,
        )


@router.post("/files/{file_id}/trash")
def trash_file(file_id: str, body: ScopedMutationIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        file = get_drive_service().trash_file(
            principal, account_id=body.account_id, space_id=body.space_id,
            file_id=file_id, generation=body.generation,
        )
        return {"file": _file_out(file, get_drive_service())}


@router.post("/files/{file_id}/restore")
def restore_file(file_id: str, body: ScopedMutationIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        file = get_drive_service().restore_file(
            principal, account_id=body.account_id, space_id=body.space_id,
            file_id=file_id, generation=body.generation,
        )
        return {"file": _file_out(file, get_drive_service())}


@router.post("/folders/{folder_id}/trash")
def trash_folder(folder_id: str, body: ScopedMutationIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        folder = get_drive_service().trash_folder(
            principal, account_id=body.account_id, space_id=body.space_id,
            folder_id=folder_id, generation=body.generation,
        )
        return {"folder": _folder_out(folder)}


@router.post("/folders/{folder_id}/restore")
def restore_folder(folder_id: str, body: ScopedMutationIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        folder = get_drive_service().restore_folder(
            principal, account_id=body.account_id, space_id=body.space_id,
            folder_id=folder_id, generation=body.generation,
        )
        return {"folder": _folder_out(folder)}


@router.post("/files/{file_id}/indexing")
def set_indexing(file_id: str, body: IndexingIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        file = get_drive_service().set_indexing(
            principal, account_id=body.account_id, space_id=body.space_id,
            file_id=file_id, generation=body.generation, enabled=body.enabled,
        )
        return {"file": _file_out(file, get_drive_service())}


@router.post("/files/{file_id}/approve")
def approve_file(file_id: str, body: ScopedMutationIn, principal: Principal = Depends(_human)):
    with _drive_errors():
        file = get_drive_service().approve_file(
            principal, account_id=body.account_id, space_id=body.space_id,
            file_id=file_id, generation=body.generation,
        )
        return {"file": _file_out(file, get_drive_service())}


@router.post("/files/{file_id}/permanent-delete")
def permanently_delete_file(
    file_id: str, body: PermanentDeleteIn, principal: Principal = Depends(_human),
):
    with _drive_errors():
        return get_drive_service().permanently_delete_file(
            principal, account_id=body.account_id, space_id=body.space_id,
            file_id=file_id, generation=body.generation, reason=body.reason,
        )


def _upload_out(upload) -> dict:
    return {
        "id": upload.id,
        "account_id": upload.account_id,
        "space_id": upload.space_id,
        "folder_id": upload.folder_id,
        "name": upload.name,
        "size_bytes": upload.size_bytes,
        "status": upload.status,
        "bytes_received": upload.bytes_received,
        "sha256": upload.sha256,
        "media_type": upload.media_type,
        "file_id": upload.file_id,
        "revision_id": upload.revision_id,
        "expires_at": upload.expires_at,
        "error": upload.error,
    }


def _legacy_entries(documents: list[dict]) -> list[dict]:
    return [{
        "kind": "file",
        "id": f"legacy_{row['doc_id']}",
        "legacy": True,
        "name": row.get("title") or "Untitled",
        "account_id": row.get("account_id", ""),
        "space_id": row.get("space_id", ""),
        "parent_folder_id": "",
        "classification": row.get("classification", "internal"),
        "location": row.get("location", "global"),
        "category": row.get("category", "general"),
        "index_status": "indexed",
        "desired_indexed": True,
        "generation": 1,
        "size_bytes": 0,
        "media_type": "application/octet-stream",
        "updated_at": "",
        "trashed_at": "",
        "original_unavailable": True,
    } for row in documents]


def _capabilities(principal: Principal) -> dict:
    mode = (get_settings().drive_policy_mode or "").strip().lower()
    return {
        "can_upload": principal.is_employee and mode != "disabled",
        "can_create_folder": principal.is_employee and mode != "disabled",
        "can_review": principal.is_employee and mode == "storage_and_indexing",
        "can_manage_labels": principal.is_employee and mode != "disabled",
        "can_index": principal.is_employee and mode == "storage_and_indexing",
        "can_permanently_delete": principal.role_id == "admin",
        "policy_mode": mode,
    }


def _audience_options(principal: Principal, service, account_id: str, space_id: str) -> dict:
    classifications = [
        item.name.lower() for item in Classification
        if Classification.INTERNAL <= item <= principal.clearance
    ]
    locations = ["global"]
    locations.extend(
        LOCATIONS if principal.locations is None
        else sorted(item for item in principal.locations if item and item != "global")
    )
    groups = [
        row for row in service.platform_store.list_access_groups(account_id, space_id)
        if row.status == "active" and row.space_id in {"", space_id}
        and (principal.categories is None or row.id in principal.categories)
    ]
    return {
        "classifications": classifications,
        "locations": list(dict.fromkeys(locations)),
        "departments": [
            {"id": "general", "name": "Everyone"},
            *({"id": row.id, "name": row.name} for row in groups),
        ],
    }

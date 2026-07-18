"""Thread-safe optional JSON-backed Drive metadata store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, replace
from typing import Mapping, Optional, Sequence

from app.drive.base import (
    DEFAULT_PAGE_SIZE,
    MAX_FOLDER_DEPTH,
    DriveConflictError,
    DriveEntryPage,
    DriveFile,
    DriveFolder,
    DriveGenerationConflict,
    DriveLimitError,
    DriveProjectionResult,
    DriveRevision,
    DriveTreeMutationResult,
    DriveUploadSession,
    bounded_page_size,
    decode_page_cursor,
    encode_page_cursor,
    now_iso,
    same_file_identity,
    same_folder_identity,
    same_revision_identity,
    validate_file,
    validate_folder,
    validate_revision,
    validate_upload,
)
from app.store.base import Chunk


class MemoryDriveStore:
    def __init__(self, vector_store, persist_path: Optional[str] = None):
        self._vector_store = vector_store
        self._persist_path = persist_path
        self._folders: dict[str, DriveFolder] = {}
        self._files: dict[str, DriveFile] = {}
        self._revisions: dict[str, DriveRevision] = {}
        self._uploads: dict[str, DriveUploadSession] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._folders = {row["id"]: DriveFolder(**row) for row in data.get("folders", [])}
            self._files = {row["id"]: DriveFile(**row) for row in data.get("files", [])}
            self._revisions = {row["id"]: DriveRevision(**row) for row in data.get("revisions", [])}
            self._uploads = {row["id"]: DriveUploadSession(**row) for row in data.get("uploads", [])}
            for row in self._folders.values():
                validate_folder(row)
            for row in self._files.values():
                validate_file(row)
            for row in self._revisions.values():
                validate_revision(row)
            for row in self._uploads.values():
                validate_upload(row)
        except Exception:
            self._folders, self._files, self._revisions, self._uploads = {}, {}, {}, {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        temporary = f"{self._persist_path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({
                "folders": [asdict(row) for row in self._folders.values()],
                "files": [asdict(row) for row in self._files.values()],
                "revisions": [asdict(row) for row in self._revisions.values()],
                "uploads": [asdict(row) for row in self._uploads.values()],
            }, handle)
        os.replace(temporary, self._persist_path)

    @staticmethod
    def _scope_matches(row, account_id: str, space_id: str) -> bool:
        return row.account_id == account_id and row.space_id == space_id

    def create_folder(self, folder: DriveFolder) -> DriveFolder:
        validate_folder(folder)
        with self._lock:
            if folder.id in self._folders:
                existing = self._folders[folder.id]
                if same_folder_identity(existing, folder):
                    return existing
                raise DriveConflictError("Folder id already exists with different metadata.")
            self._validate_parent(folder.parent_id, folder.account_id, folder.space_id)
            self._ensure_sibling_name(folder.account_id, folder.space_id, folder.parent_id, folder.name)
            timestamp = now_iso()
            stored = replace(folder, created_at=folder.created_at or timestamp, updated_at=timestamp)
            self._folders[stored.id] = stored
            self._save()
            return stored

    def get_folder(self, folder_id: str, *, account_id: str, space_id: str) -> Optional[DriveFolder]:
        if not folder_id:
            return None
        row = self._folders.get(folder_id)
        return row if row and self._scope_matches(row, account_id, space_id) else None

    def list_entries(
        self, *, account_id: str, space_id: str, folder_id: str = "", query: str = "",
        trashed: bool = False, cursor: str = "", limit: int = DEFAULT_PAGE_SIZE,
    ) -> DriveEntryPage:
        limit = bounded_page_size(limit)
        offset = decode_page_cursor(cursor)
        query = (query or "").strip().casefold()
        with self._lock:
            folders = [
                row for row in self._folders.values()
                if self._scope_matches(row, account_id, space_id)
                and (bool(row.trashed_at) == bool(trashed))
                and ((query and query in row.name.casefold()) or (not query and row.parent_id == folder_id))
            ]
            files = [
                row for row in self._files.values()
                if self._scope_matches(row, account_id, space_id)
                and (bool(row.trashed_at) == bool(trashed))
                and ((query and query in row.name.casefold()) or (not query and row.folder_id == folder_id))
            ]
            combined = [("folder", row.name.casefold(), row.id, row) for row in folders]
            combined.extend(("file", row.name.casefold(), row.id, row) for row in files)
            combined.sort(key=lambda item: (item[0] != "folder", item[1], item[2]))
            selected = combined[offset:offset + limit]
            next_cursor = encode_page_cursor(offset + limit) if len(combined) > offset + limit else ""
            return DriveEntryPage(
                folders=tuple(item[3] for item in selected if item[0] == "folder"),
                files=tuple(item[3] for item in selected if item[0] == "file"),
                next_cursor=next_cursor,
            )

    def breadcrumbs(self, folder_id: str, *, account_id: str, space_id: str) -> list[DriveFolder]:
        out: list[DriveFolder] = []
        seen: set[str] = set()
        current = folder_id
        while current:
            if current in seen or len(out) >= MAX_FOLDER_DEPTH:
                raise DriveLimitError("Folder hierarchy is cyclic or too deep.")
            seen.add(current)
            row = self.get_folder(current, account_id=account_id, space_id=space_id)
            if not row:
                raise KeyError("Folder not found.")
            out.append(row)
            current = row.parent_id
        return list(reversed(out))

    def update_folder(self, folder: DriveFolder, *, expected_generation: int) -> DriveFolder:
        validate_folder(folder)
        with self._lock:
            current = self.get_folder(folder.id, account_id=folder.account_id, space_id=folder.space_id)
            if not current:
                raise KeyError("Folder not found.")
            if current.generation != expected_generation:
                raise DriveGenerationConflict("Folder changed; refresh and try again.")
            self._validate_parent(folder.parent_id, folder.account_id, folder.space_id, moving_id=folder.id)
            self._ensure_sibling_name(
                folder.account_id, folder.space_id, folder.parent_id, folder.name, excluding_id=folder.id,
            )
            stored = replace(folder, created_at=current.created_at, updated_at=now_iso())
            self._folders[stored.id] = stored
            self._save()
            return stored

    def trash_folder_tree(
        self, *, root: DriveFolder, expected_generation: int, operation_id: str,
        timestamp: str, folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
    ) -> DriveTreeMutationResult:
        with self._lock:
            current = self.get_folder(root.id, account_id=root.account_id, space_id=root.space_id)
            if not current or current.generation != expected_generation or current.trashed_at:
                raise DriveGenerationConflict("Folder changed; refresh and try again.")
            folders, files = self._tree_rows(current, trashed=False)
            self._verify_tree_snapshot(folders, files, folder_generations, file_generations)
            updated_files: list[DriveFile] = []
            for file in files:
                self._delete_file_chunks(file)
                stored_file = replace(
                    file,
                    trashed_at=timestamp,
                    original_folder_id=file.folder_id,
                    trash_operation_id=operation_id,
                    active_doc_id="",
                    index_status="not_indexed",
                    generation=file.generation + 1,
                    updated_at=now_iso(),
                )
                self._files[file.id] = stored_file
                updated_files.append(stored_file)
            updated_folders: dict[str, DriveFolder] = {}
            for folder in folders:
                stored_folder = replace(
                    folder,
                    trashed_at=timestamp,
                    original_parent_id=folder.parent_id,
                    trash_operation_id=operation_id,
                    generation=folder.generation + 1,
                    updated_at=now_iso(),
                )
                self._folders[folder.id] = stored_folder
                updated_folders[folder.id] = stored_folder
            self._save()
            return DriveTreeMutationResult(updated_folders[current.id], tuple(updated_files))

    def restore_folder_tree(
        self, *, root: DriveFolder, expected_generation: int, operation_id: str,
        folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
        indexing_enabled: bool = True,
    ) -> DriveTreeMutationResult:
        with self._lock:
            current = self.get_folder(root.id, account_id=root.account_id, space_id=root.space_id)
            if (
                not current or current.generation != expected_generation or not current.trashed_at
                or current.trash_operation_id != operation_id
            ):
                raise DriveGenerationConflict("Folder changed; refresh and try again.")
            folders, files = self._tree_rows(current, trashed=True, operation_id=operation_id)
            self._verify_tree_snapshot(folders, files, folder_generations, file_generations)
            if root.parent_id:
                self._validate_parent(root.parent_id, root.account_id, root.space_id, moving_id=root.id)
            updated_folders: dict[str, DriveFolder] = {}
            for folder in folders:
                stored_folder = replace(
                    folder,
                    parent_id=root.parent_id if folder.id == root.id else folder.parent_id,
                    trashed_at="",
                    original_parent_id="",
                    trash_operation_id="",
                    generation=folder.generation + 1,
                    updated_at=now_iso(),
                )
                self._folders[folder.id] = stored_folder
                updated_folders[folder.id] = stored_folder
            updated_files: list[DriveFile] = []
            for file in files:
                stored_file = replace(
                    file,
                    trashed_at="",
                    original_folder_id="",
                    trash_operation_id="",
                    index_status=(
                        "queued" if file.desired_indexed and indexing_enabled else "not_indexed"
                    ),
                    generation=file.generation + 1,
                    updated_at=now_iso(),
                )
                self._files[file.id] = stored_file
                updated_files.append(stored_file)
            self._save()
            return DriveTreeMutationResult(updated_folders[current.id], tuple(updated_files))

    def create_file(self, file: DriveFile) -> DriveFile:
        validate_file(file)
        with self._lock:
            if file.id in self._files:
                existing = self._files[file.id]
                if same_file_identity(existing, file):
                    return existing
                raise DriveConflictError("File id already exists with different metadata.")
            self._validate_parent(file.folder_id, file.account_id, file.space_id)
            timestamp = now_iso()
            stored = replace(file, created_at=file.created_at or timestamp, updated_at=timestamp)
            self._files[stored.id] = stored
            self._save()
            return stored

    def get_file(self, file_id: str, *, account_id: str, space_id: str) -> Optional[DriveFile]:
        row = self._files.get(file_id)
        return row if row and self._scope_matches(row, account_id, space_id) else None

    def update_file(self, file: DriveFile, *, expected_generation: int) -> DriveFile:
        validate_file(file)
        with self._lock:
            current = self.get_file(file.id, account_id=file.account_id, space_id=file.space_id)
            if not current:
                raise KeyError("File not found.")
            if current.generation != expected_generation:
                raise DriveGenerationConflict("File changed; refresh and try again.")
            self._validate_parent(file.folder_id, file.account_id, file.space_id)
            if not file.active_doc_id:
                self._delete_file_chunks(current)
            stored = replace(file, created_at=current.created_at, updated_at=now_iso())
            self._files[stored.id] = stored
            self._save()
            return stored

    def create_revision(self, revision: DriveRevision) -> DriveRevision:
        validate_revision(revision)
        with self._lock:
            parent = self.get_file(
                revision.file_id, account_id=revision.account_id, space_id=revision.space_id,
            )
            if not parent or parent.tenant_id != revision.tenant_id:
                raise KeyError("Revision parent file not found.")
            if revision.id in self._revisions:
                existing = self._revisions[revision.id]
                if same_revision_identity(existing, revision):
                    return existing
                raise DriveConflictError("Revision id already exists.")
            if revision.upload_session_id:
                existing = next((
                    row for row in self._revisions.values()
                    if row.upload_session_id == revision.upload_session_id
                ), None)
                if existing:
                    if same_revision_identity(existing, revision):
                        return existing
                    raise DriveConflictError("Upload session already created a different revision.")
            stored = replace(revision, created_at=revision.created_at or now_iso())
            self._revisions[stored.id] = stored
            self._save()
            return stored

    def _tree_rows(
        self, root: DriveFolder, *, trashed: bool, operation_id: str = "",
    ) -> tuple[list[DriveFolder], list[DriveFile]]:
        folder_ids = {root.id}
        while True:
            children = {
                row.id for row in self._folders.values()
                if row.account_id == root.account_id and row.space_id == root.space_id
                and row.parent_id in folder_ids and row.id not in folder_ids
                and bool(row.trashed_at) == trashed
                and (not operation_id or row.trash_operation_id == operation_id)
            }
            if not children:
                break
            folder_ids.update(children)
            if len(folder_ids) > 10_000:
                raise DriveLimitError("Folder tree exceeds the safe mutation limit.")
        folders = [self._folders[item] for item in folder_ids]
        files = [
            row for row in self._files.values()
            if row.account_id == root.account_id and row.space_id == root.space_id
            and row.folder_id in folder_ids and bool(row.trashed_at) == trashed
            and (not operation_id or row.trash_operation_id == operation_id)
        ]
        return folders, files

    @staticmethod
    def _verify_tree_snapshot(
        folders: Sequence[DriveFolder], files: Sequence[DriveFile],
        folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
    ) -> None:
        actual_folders = {row.id: row.generation for row in folders}
        actual_files = {row.id: row.generation for row in files}
        if actual_folders != dict(folder_generations) or actual_files != dict(file_generations):
            raise DriveGenerationConflict("Folder contents changed; refresh and try again.")

    def get_revision(
        self, revision_id: str, *, account_id: str, space_id: str,
    ) -> Optional[DriveRevision]:
        row = self._revisions.get(revision_id)
        return row if row and self._scope_matches(row, account_id, space_id) else None

    def list_revisions(self, file_id: str, *, account_id: str, space_id: str) -> list[DriveRevision]:
        rows = [
            row for row in self._revisions.values()
            if row.file_id == file_id and self._scope_matches(row, account_id, space_id)
        ]
        return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)

    def create_upload(self, upload: DriveUploadSession) -> DriveUploadSession:
        validate_upload(upload)
        with self._lock:
            existing = self.get_upload_by_idempotency(
                account_id=upload.account_id, space_id=upload.space_id,
                created_by=upload.created_by, idempotency_key=upload.idempotency_key,
            )
            if existing:
                return existing
            if upload.id in self._uploads:
                raise DriveConflictError("Upload id already exists.")
            timestamp = now_iso()
            stored = replace(upload, created_at=upload.created_at or timestamp, updated_at=timestamp)
            self._uploads[stored.id] = stored
            self._save()
            return stored

    def get_upload(self, upload_id: str, *, tenant_id: str = "") -> Optional[DriveUploadSession]:
        row = self._uploads.get(upload_id)
        if not row or (tenant_id and row.tenant_id != tenant_id):
            return None
        return row

    def get_upload_by_idempotency(
        self, *, account_id: str, space_id: str, created_by: str, idempotency_key: str,
    ) -> Optional[DriveUploadSession]:
        return next((
            row for row in self._uploads.values()
            if row.account_id == account_id and row.space_id == space_id
            and row.created_by == created_by and row.idempotency_key == idempotency_key
        ), None)

    def update_upload(self, upload: DriveUploadSession) -> DriveUploadSession:
        validate_upload(upload)
        with self._lock:
            current = self._uploads.get(upload.id)
            if not current or current.tenant_id != upload.tenant_id:
                raise KeyError("Upload session not found.")
            stored = replace(upload, created_at=current.created_at, updated_at=now_iso())
            self._uploads[stored.id] = stored
            self._save()
            return stored

    def list_expired_uploads(
        self, *, tenant_id: str, account_id: str, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]:
        bounded = max(1, min(int(limit), 5_000))
        terminal = {"completed", "failed", "expired"}
        rows = [
            row for row in self._uploads.values()
            if row.tenant_id == tenant_id and row.account_id == account_id
            and row.status not in terminal
            and (not row.expires_at or row.expires_at <= before)
        ]
        return sorted(rows, key=lambda row: (row.expires_at, row.id))[:bounded]

    def publish_projection(
        self, *, file_id: str, revision_id: str, generation: int,
        account_id: str, space_id: str, chunks: Sequence[Chunk],
    ) -> DriveProjectionResult:
        with self._lock:
            file = self.get_file(file_id, account_id=account_id, space_id=space_id)
            if not file or file.current_revision_id != revision_id or file.generation != generation:
                raise DriveGenerationConflict("Drive indexing work is stale.")
            if file.trashed_at or not file.desired_indexed or file.approval_status not in {"approved", "not_required"}:
                raise DriveGenerationConflict("Drive file is no longer eligible for indexing.")
            doc_id = chunks[0].doc_id if chunks else ""
            if not doc_id:
                raise ValueError("A Drive projection must contain chunks.")
            self._delete_file_chunks(file)
            self._vector_store.add(list(chunks))
            stored = replace(file, active_doc_id=doc_id, index_status="indexed", updated_at=now_iso())
            self._files[file.id] = stored
            self._save()
            return DriveProjectionResult(stored, len(chunks))

    def unpublish(self, *, file_id: str, account_id: str, space_id: str, generation: int) -> DriveFile:
        with self._lock:
            file = self.get_file(file_id, account_id=account_id, space_id=space_id)
            if not file or file.generation != generation:
                raise DriveGenerationConflict("Drive file changed.")
            self._delete_file_chunks(file)
            stored = replace(file, active_doc_id="", index_status="not_indexed", updated_at=now_iso())
            self._files[file.id] = stored
            self._save()
            return stored

    def list_pending_review(self, *, account_id: str, space_id: str = "") -> list[DriveFile]:
        rows = [
            row for row in self._files.values()
            if row.account_id == account_id and (not space_id or row.space_id == space_id)
            and row.approval_status == "pending" and not row.trashed_at
        ]
        return sorted(rows, key=lambda row: (row.updated_at, row.id))

    def delete_file(self, *, file_id: str, account_id: str, space_id: str) -> dict[str, int]:
        with self._lock:
            file = self.get_file(file_id, account_id=account_id, space_id=space_id)
            if not file:
                return {"files": 0, "revisions": 0, "chunks": 0}
            chunks = self._delete_file_chunks(file)
            revision_ids = [key for key, row in self._revisions.items() if row.file_id == file.id]
            for key in revision_ids:
                self._revisions.pop(key, None)
            self._files.pop(file.id, None)
            self._save()
            return {"files": 1, "revisions": len(revision_ids), "chunks": chunks}

    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict:
        def match(row) -> bool:
            return (
                row.tenant_id == tenant_id
                and row.account_id == account_id
                and (not space_id or row.space_id == space_id)
            )

        return {
            "folders": [asdict(row) for row in self._folders.values() if match(row)],
            "files": [asdict(row) for row in self._files.values() if match(row)],
            "revisions": [asdict(row) for row in self._revisions.values() if match(row)],
            "uploads": [asdict(row) for row in self._uploads.values() if match(row)],
        }

    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]:
        def match(row) -> bool:
            return (
                row.tenant_id == tenant_id
                and row.account_id == account_id
                and (not space_id or row.space_id == space_id)
            )

        with self._lock:
            files = [row for row in self._files.values() if match(row)]
            for row in files:
                self._delete_file_chunks(row)
            counts = {
                "folders": sum(1 for row in self._folders.values() if match(row)),
                "files": len(files),
                "revisions": sum(1 for row in self._revisions.values() if match(row)),
                "uploads": sum(1 for row in self._uploads.values() if match(row)),
            }
            self._folders = {key: row for key, row in self._folders.items() if not match(row)}
            self._files = {key: row for key, row in self._files.items() if not match(row)}
            self._revisions = {key: row for key, row in self._revisions.items() if not match(row)}
            self._uploads = {key: row for key, row in self._uploads.items() if not match(row)}
            self._save()
            return counts

    def _delete_file_chunks(self, file: DriveFile) -> int:
        removed = 0
        delete_by_metadata = getattr(self._vector_store, "delete_by_metadata", None)
        if callable(delete_by_metadata):
            removed += int(delete_by_metadata("drive_file_id", file.id))
        if file.active_doc_id:
            removed += int(self._vector_store.delete_document(file.active_doc_id))
        return removed

    def _validate_parent(
        self, parent_id: str, account_id: str, space_id: str, *, moving_id: str = "",
    ) -> None:
        if not parent_id:
            return
        if parent_id == moving_id:
            raise DriveConflictError("A folder cannot contain itself.")
        parent = self.get_folder(parent_id, account_id=account_id, space_id=space_id)
        if not parent or parent.trashed_at:
            raise KeyError("Parent folder not found.")
        ancestors = self.breadcrumbs(parent_id, account_id=account_id, space_id=space_id)
        if any(row.id == moving_id for row in ancestors):
            raise DriveConflictError("A folder cannot move inside its descendant.")
        if len(ancestors) >= MAX_FOLDER_DEPTH:
            raise DriveLimitError("Folder hierarchy is too deep.")

    def _ensure_sibling_name(
        self, account_id: str, space_id: str, parent_id: str, name: str, *, excluding_id: str = "",
    ) -> None:
        normalized = name.casefold()
        if any(
            row.id != excluding_id and row.account_id == account_id and row.space_id == space_id
            and row.parent_id == parent_id and not row.trashed_at and row.name.casefold() == normalized
            for row in self._folders.values()
        ):
            raise DriveConflictError("A folder with this name already exists here.")

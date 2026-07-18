"""Thread-safe optional JSON-backed Drive metadata store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Sequence
from uuid import uuid5, NAMESPACE_URL

from app.drive.base import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_DRIVE_QUARANTINE_LIMIT_BYTES,
    DRIVE_MALWARE_POLICY_EPOCH,
    MAX_FOLDER_DEPTH,
    DriveConflictError,
    DriveEntryPage,
    DriveFile,
    DriveFileListDetail,
    DriveFolder,
    DriveGenerationConflict,
    DriveLimitError,
    DriveMalwareCompletion,
    DriveMalwareJobSpec,
    DriveMalwareOperationalCounts,
    DriveMalwareReconcileResult,
    DriveMalwareScan,
    DriveProjectionResult,
    DriveQuarantineCapacityError,
    DriveRevision,
    DriveQuarantineUsage,
    DriveQuarantinedCompletion,
    DriveTreeMutationResult,
    DriveUploadSession,
    MalwareActivationState,
    ScannerRuntimeStatus,
    authoritative_malware_scan,
    bounded_page_size,
    decode_page_cursor,
    drive_ingest_job_id,
    encode_page_cursor,
    is_clean_attestation,
    malware_scan_matches_revision,
    normalize_file_list_detail_revision_ids,
    now_iso,
    same_file_identity,
    same_folder_identity,
    same_revision_identity,
    validate_file,
    validate_folder,
    validate_malware_activation_state,
    validate_malware_scan,
    validate_revision,
    validate_scanner_runtime_status,
    validate_upload,
)
from app.drive.malware.base import ScanVerdict
from app.store.base import Chunk


class MemoryDriveStore:
    def __init__(
        self, vector_store, persist_path: Optional[str] = None,
        malware_job_lookup: Optional[Callable[[str], object | None]] = None,
        quarantine_limit_bytes: int = DEFAULT_DRIVE_QUARANTINE_LIMIT_BYTES,
    ):
        self._vector_store = vector_store
        self._persist_path = persist_path
        self._folders: dict[str, DriveFolder] = {}
        self._files: dict[str, DriveFile] = {}
        self._revisions: dict[str, DriveRevision] = {}
        self._uploads: dict[str, DriveUploadSession] = {}
        self._malware_scans: dict[str, DriveMalwareScan] = {}
        self._scanner_runtime: dict[str, ScannerRuntimeStatus] = {}
        self._malware_job_outbox: dict[str, DriveMalwareJobSpec] = {}
        self._malware_activation = MalwareActivationState()
        self._malware_job_lookup = malware_job_lookup
        self._quarantine_limit_bytes = int(quarantine_limit_bytes)
        if self._quarantine_limit_bytes <= 0:
            raise ValueError("Quarantine limit must be positive.")
        self._malware_lease_tokens: dict[str, str] = {}
        self._lock = threading.RLock()
        self._load()

    def bind_malware_job_authority(
        self, job_lookup: Callable[[str], object | None],
    ) -> None:
        """Bind the live queue authority used by fenced local scan operations."""

        self._malware_job_lookup = job_lookup

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
            self._malware_scans = {
                row["id"]: DriveMalwareScan(**row) for row in data.get("malware_scans", [])
            }
            self._scanner_runtime = {
                f"{row['tenant_id']}:{row['worker_id']}": ScannerRuntimeStatus(**row)
                for row in data.get("scanner_runtime", [])
            }
            self._malware_job_outbox = {
                row["job_id"]: DriveMalwareJobSpec(**row)
                for row in data.get("malware_job_outbox", [])
            }
            self._malware_activation = MalwareActivationState(
                **data.get("malware_activation", {})
            )
            for row in self._folders.values():
                validate_folder(row)
            for row in self._files.values():
                validate_file(row)
            for row in self._revisions.values():
                validate_revision(row)
            for row in self._uploads.values():
                validate_upload(row)
            for row in self._malware_scans.values():
                validate_malware_scan(row)
            for row in self._scanner_runtime.values():
                validate_scanner_runtime_status(row)
            validate_malware_activation_state(self._malware_activation)
        except Exception:
            self._folders, self._files, self._revisions, self._uploads = {}, {}, {}, {}
            self._malware_scans, self._scanner_runtime, self._malware_job_outbox = {}, {}, {}
            self._malware_activation = MalwareActivationState()

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
                "malware_scans": [asdict(row) for row in self._malware_scans.values()],
                "scanner_runtime": [asdict(row) for row in self._scanner_runtime.values()],
                "malware_job_outbox": [asdict(row) for row in self._malware_job_outbox.values()],
                "malware_activation": asdict(self._malware_activation),
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

    def get_file_list_details(
        self, *, account_id: str, space_id: str, revision_ids: Sequence[str],
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    ) -> Mapping[str, DriveFileListDetail]:
        requested = normalize_file_list_detail_revision_ids(revision_ids)
        if policy_epoch < 1:
            raise ValueError("Drive malware policy epoch must be positive.")
        if not requested:
            return {}
        with self._lock:
            revisions: dict[str, DriveRevision] = {}
            for revision_id in requested:
                revision = self._revisions.get(revision_id)
                if not revision or not self._scope_matches(revision, account_id, space_id):
                    continue
                file = self._files.get(revision.file_id)
                if (
                    not file
                    or not self._scope_matches(file, account_id, space_id)
                    or file.current_revision_id != revision.id
                ):
                    continue
                revisions[revision.id] = revision

            authoritative: dict[str, DriveMalwareScan] = {}
            for scan in self._malware_scans.values():
                if (
                    scan.revision_id not in revisions
                    or scan.policy_epoch != policy_epoch
                    or not self._scope_matches(scan, account_id, space_id)
                ):
                    continue
                current = authoritative.get(scan.revision_id)
                if current is None or (scan.attempt_sequence, scan.id) > (
                    current.attempt_sequence,
                    current.id,
                ):
                    authoritative[scan.revision_id] = scan

            details: dict[str, DriveFileListDetail] = {}
            for revision_id, revision in revisions.items():
                scan = authoritative.get(revision_id)
                if scan and not malware_scan_matches_revision(
                    revision,
                    scan,
                    policy_epoch=policy_epoch,
                ):
                    scan = None
                details[revision_id] = DriveFileListDetail(
                    revision=revision,
                    malware_scan=scan,
                )
            return details

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

    def reserve_upload(self, upload: DriveUploadSession) -> DriveUploadSession:
        candidate = replace(
            upload,
            quarantine_reserved_bytes=upload.size_bytes,
            reservation_state="reserved",
            reservation_expires_at=upload.reservation_expires_at or upload.expires_at,
        )
        validate_upload(candidate)
        with self._lock:
            existing = self.get_upload_by_idempotency(
                account_id=candidate.account_id,
                space_id=candidate.space_id,
                created_by=candidate.created_by,
                idempotency_key=candidate.idempotency_key,
            )
            if existing:
                return existing
            usage = self.reconcile_quarantine_capacity().usage_bytes
            if usage + candidate.quarantine_reserved_bytes > self._quarantine_limit_bytes:
                raise DriveQuarantineCapacityError()
            return self.create_upload(candidate)

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

    def release_upload_reservation(
        self, upload_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> DriveUploadSession:
        with self._lock:
            current = self._uploads.get(upload_id)
            if not current or (
                current.tenant_id != tenant_id
                or current.account_id != account_id
                or current.space_id != space_id
            ):
                raise KeyError("Upload session not found.")
            if current.reservation_state == "released":
                return current
            stored = replace(
                current,
                quarantine_reserved_bytes=0,
                reservation_state="released",
                reservation_expires_at="",
                updated_at=now_iso(),
            )
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

    def list_expired_uploads_for_maintenance(
        self, *, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]:
        bounded = max(1, min(int(limit), 5_000))
        terminal = {"completed", "failed", "expired"}
        rows = [
            row for row in self._uploads.values()
            if row.status not in terminal
            and row.reservation_state == "reserved"
            and (not row.expires_at or row.expires_at <= before)
        ]
        return sorted(rows, key=lambda row: (row.expires_at, row.id))[:bounded]

    def complete_upload_quarantined(
        self, *, upload: DriveUploadSession, file: DriveFile, revision: DriveRevision,
        scan: DriveMalwareScan, scan_job_id: str, scan_job_max_attempts: int = 5,
    ) -> DriveQuarantinedCompletion:
        """Atomically transfer a reservation into a quarantined revision/outbox."""

        validate_upload(upload)
        validate_file(file)
        validate_revision(revision)
        validate_malware_scan(scan)
        if scan.status != "pending" or scan.origin != "upload":
            raise ValueError("Upload completion requires a pending upload malware attempt.")
        if not scan_job_id or scan_job_max_attempts < 1:
            raise ValueError("A deterministic malware scan job is required.")
        if not (
            upload.id == revision.upload_session_id
            and upload.id == self._uploads.get(upload.id, upload).id
            and file.id == revision.file_id == scan.file_id
            and revision.id == scan.revision_id
            and revision.sha256 == scan.revision_sha256 == upload.sha256
            and revision.size_bytes == scan.revision_size_bytes == upload.size_bytes
            and (file.tenant_id, file.account_id, file.space_id)
            == (upload.tenant_id, upload.account_id, upload.space_id)
            == (revision.tenant_id, revision.account_id, revision.space_id)
            == (scan.tenant_id, scan.account_id, scan.space_id)
        ):
            raise DriveConflictError("Quarantined upload records do not describe one revision.")

        with self._lock:
            current_upload = self._uploads.get(upload.id)
            if not current_upload:
                raise KeyError("Upload session not found.")
            if current_upload.status == "completed":
                current_file = self._files.get(current_upload.file_id)
                current_revision = self._revisions.get(current_upload.revision_id)
                current_scan = self.get_authoritative_malware_scan(
                    current_upload.revision_id,
                    account_id=current_upload.account_id,
                    space_id=current_upload.space_id,
                )
                if current_file and current_revision and current_scan:
                    self._verify_existing_malware_job_spec(DriveMalwareJobSpec(
                        job_id=current_scan.job_id,
                        tenant_id=current_scan.tenant_id,
                        account_id=current_scan.account_id,
                        space_id=current_scan.space_id,
                        scan_id=current_scan.id,
                        revision_id=current_scan.revision_id,
                        origin=current_scan.origin,
                        requested_by=current_upload.created_by,
                        max_attempts=scan_job_max_attempts,
                        run_after=current_scan.next_attempt_at or current_scan.created_at,
                        idempotency_key=f"drive-malware:{current_scan.id}",
                    ))
                    return DriveQuarantinedCompletion(
                        current_upload, current_file, current_revision, current_scan,
                        current_scan.job_id,
                    )
                raise DriveConflictError("Completed upload has incomplete quarantine metadata.")
            if current_upload.status not in {"uploaded", "completing"}:
                raise DriveConflictError("Upload is not ready for quarantine completion.")
            if current_upload.reservation_state != "reserved":
                raise DriveConflictError("Upload has no active quarantine reservation.")

            snapshot = (
                dict(self._files), dict(self._revisions), dict(self._uploads),
                dict(self._malware_scans), dict(self._malware_job_outbox),
            )
            try:
                self._validate_parent(file.folder_id, file.account_id, file.space_id)
                timestamp = now_iso()
                stored_file = replace(
                    file,
                    index_status="awaiting_scan" if file.desired_indexed else "not_indexed",
                    active_doc_id="",
                    created_at=file.created_at or timestamp,
                    updated_at=timestamp,
                )
                existing_file = self._files.get(stored_file.id)
                if existing_file and not same_file_identity(existing_file, stored_file):
                    raise DriveConflictError("File id already exists with different metadata.")
                stored_file = existing_file or stored_file
                self._files[stored_file.id] = stored_file

                existing_revision = self._revisions.get(revision.id)
                if existing_revision and not same_revision_identity(existing_revision, revision):
                    raise DriveConflictError("Revision id already exists with different metadata.")
                stored_revision = existing_revision or replace(
                    revision, created_at=revision.created_at or timestamp
                )
                self._revisions[stored_revision.id] = stored_revision

                stored_scan = replace(
                    scan,
                    job_id=scan_job_id,
                    created_at=scan.created_at or timestamp,
                    updated_at=timestamp,
                )
                validate_malware_scan(stored_scan)
                duplicate = next((
                    row for row in self._malware_scans.values()
                    if row.revision_id == stored_scan.revision_id
                    and row.policy_epoch == stored_scan.policy_epoch
                    and row.attempt_sequence == stored_scan.attempt_sequence
                ), None)
                if duplicate and not self._same_malware_attempt_identity(duplicate, stored_scan):
                    raise DriveConflictError("Malware attempt sequence already exists.")
                stored_scan = duplicate or stored_scan
                self._malware_scans[stored_scan.id] = stored_scan

                stored_upload = replace(
                    current_upload,
                    status="completed",
                    file_id=stored_file.id,
                    revision_id=stored_revision.id,
                    reservation_state="transferred",
                    reservation_expires_at="",
                    error="",
                    updated_at=timestamp,
                )
                self._uploads[stored_upload.id] = stored_upload
                self._record_malware_job_spec(DriveMalwareJobSpec(
                    job_id=scan_job_id,
                    tenant_id=scan.tenant_id,
                    account_id=scan.account_id,
                    space_id=scan.space_id,
                    scan_id=stored_scan.id,
                    revision_id=revision.id,
                    origin=scan.origin,
                    requested_by=upload.created_by,
                    max_attempts=scan_job_max_attempts,
                    run_after=scan.next_attempt_at or timestamp,
                    idempotency_key=f"drive-malware:{stored_scan.id}",
                ))
                self._save()
            except Exception:
                (
                    self._files, self._revisions, self._uploads,
                    self._malware_scans, self._malware_job_outbox,
                ) = snapshot
                raise
            return DriveQuarantinedCompletion(
                stored_upload, stored_file, stored_revision, stored_scan, scan_job_id
            )

    def create_malware_scan(self, scan: DriveMalwareScan) -> DriveMalwareScan:
        validate_malware_scan(scan)
        with self._lock:
            revision = self.get_revision(
                scan.revision_id, account_id=scan.account_id, space_id=scan.space_id
            )
            if not revision or (
                revision.file_id != scan.file_id
                or revision.tenant_id != scan.tenant_id
                or revision.sha256 != scan.revision_sha256
                or revision.size_bytes != scan.revision_size_bytes
            ):
                raise DriveConflictError("Malware attempt does not match its immutable revision.")
            duplicate = next((
                row for row in self._malware_scans.values()
                if row.revision_id == scan.revision_id
                and row.policy_epoch == scan.policy_epoch
                and row.attempt_sequence == scan.attempt_sequence
            ), None)
            if duplicate:
                if self._same_malware_attempt_identity(duplicate, scan):
                    return duplicate
                raise DriveConflictError("Malware attempt sequence already exists.")
            if scan.id in self._malware_scans:
                raise DriveConflictError("Malware scan id already exists.")
            timestamp = now_iso()
            stored = replace(scan, created_at=scan.created_at or timestamp, updated_at=timestamp)
            self._malware_scans[stored.id] = stored
            self._save()
            return stored

    def get_malware_scan(
        self, scan_id: str, *, account_id: str, space_id: str,
    ) -> Optional[DriveMalwareScan]:
        row = self._malware_scans.get(scan_id)
        return row if row and self._scope_matches(row, account_id, space_id) else None

    def list_malware_scans(
        self, revision_id: str, *, account_id: str, space_id: str,
    ) -> list[DriveMalwareScan]:
        rows = [
            row for row in self._malware_scans.values()
            if row.revision_id == revision_id and self._scope_matches(row, account_id, space_id)
        ]
        return sorted(rows, key=lambda row: (row.policy_epoch, row.attempt_sequence, row.id))

    def get_authoritative_malware_scan(
        self, revision_id: str, *, account_id: str, space_id: str,
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    ) -> Optional[DriveMalwareScan]:
        return authoritative_malware_scan(
            self.list_malware_scans(
                revision_id, account_id=account_id, space_id=space_id
            ),
            policy_epoch=policy_epoch,
        )

    def malware_scan_job_status(self, scan: DriveMalwareScan) -> str:
        with self._lock:
            job = self._lookup_malware_job(scan.job_id)
            if job:
                self._validate_malware_job_identity(scan, job)
                return str(getattr(job, "status", ""))
            pending = self._malware_job_outbox.get(scan.job_id)
            if pending:
                self._validate_malware_job_spec_identity(scan, pending)
                return "queued"
            return ""

    def update_malware_scan(
        self, scan: DriveMalwareScan, *, expected_status: str,
    ) -> DriveMalwareScan:
        validate_malware_scan(scan)
        with self._lock:
            current = self._malware_scans.get(scan.id)
            if not current or not self._scope_matches(current, scan.account_id, scan.space_id):
                raise KeyError("Malware scan not found.")
            if current.status != expected_status:
                raise DriveConflictError("Malware scan state changed.")
            if not self._same_malware_attempt_identity(current, scan):
                raise DriveConflictError("Malware attempt identity is immutable.")
            allowed = {
                "pending": {"pending", "scanning", "scan_error"},
                "rescan_required": {"rescan_required", "scanning", "scan_error"},
                "scanning": {"scanning", "clean", "infected", "scan_error"},
                "clean": {"clean"},
                "infected": {"infected"},
                "scan_error": {"scan_error"},
            }
            if scan.status not in allowed[current.status]:
                raise DriveConflictError("Illegal malware scan state transition.")
            stored = replace(scan, created_at=current.created_at, updated_at=now_iso())
            self._malware_scans[stored.id] = stored
            self._save()
            return stored

    def upsert_scanner_runtime_status(
        self, status: ScannerRuntimeStatus,
    ) -> ScannerRuntimeStatus:
        validate_scanner_runtime_status(status)
        with self._lock:
            key = f"{status.tenant_id}:{status.worker_id}"
            current = self._scanner_runtime.get(key)
            timestamp = now_iso()
            stored = replace(
                status,
                created_at=(current.created_at if current else status.created_at) or timestamp,
                updated_at=timestamp,
            )
            self._scanner_runtime[key] = stored
            self._save()
            return stored

    def list_scanner_runtime_status(self, *, tenant_id: str) -> list[ScannerRuntimeStatus]:
        rows = [row for row in self._scanner_runtime.values() if row.tenant_id == tenant_id]
        return sorted(rows, key=lambda row: row.worker_id)

    def get_malware_activation_state(self) -> MalwareActivationState:
        return self._malware_activation

    def quarantine_limit_bytes(self) -> int:
        return self._quarantine_limit_bytes

    def quarantine_usage_bytes(self) -> int:
        return self.reconcile_quarantine_capacity().usage_bytes

    def reconcile_quarantine_capacity(self) -> DriveQuarantineUsage:
        with self._lock:
            reserved = sum(
                row.quarantine_reserved_bytes for row in self._uploads.values()
                if row.reservation_state == "reserved"
            )
            quarantined = 0
            for revision in self._revisions.values():
                scan = self.get_authoritative_malware_scan(
                    revision.id,
                    account_id=revision.account_id,
                    space_id=revision.space_id,
                )
                if not scan or scan.status != "clean":
                    quarantined += revision.size_bytes
            return DriveQuarantineUsage(
                usage_bytes=reserved + quarantined,
                reserved_bytes=reserved,
                quarantined_bytes=quarantined,
            )

    def malware_operational_counts(
        self, *, tenant_id: str,
    ) -> DriveMalwareOperationalCounts:
        with self._lock:
            reserved = sum(
                row.quarantine_reserved_bytes for row in self._uploads.values()
                if row.tenant_id == tenant_id and row.reservation_state == "reserved"
            )
            latest: dict[tuple[str, int], DriveMalwareScan] = {}
            for scan in self._malware_scans.values():
                if scan.tenant_id != tenant_id:
                    continue
                key = (scan.revision_id, scan.policy_epoch)
                current = latest.get(key)
                if current is None or scan.attempt_sequence > current.attempt_sequence:
                    latest[key] = scan
            pending = sum(
                1 for scan in latest.values()
                if scan.policy_epoch == DRIVE_MALWARE_POLICY_EPOCH
                and scan.status in {"pending", "scanning", "scan_error", "rescan_required"}
            )
            quarantined = 0
            for revision in self._revisions.values():
                if revision.tenant_id != tenant_id:
                    continue
                scan = latest.get((revision.id, DRIVE_MALWARE_POLICY_EPOCH))
                if not scan or scan.status != "clean":
                    quarantined += revision.size_bytes
            return DriveMalwareOperationalCounts(
                pending_count=pending,
                quarantine_usage_bytes=reserved + quarantined,
                quarantine_reserved_bytes=reserved,
                quarantined_revision_bytes=quarantined,
            )

    def list_pending_malware_job_specs(
        self, *, limit: int = 100,
    ) -> list[DriveMalwareJobSpec]:
        bounded = max(1, min(int(limit), 1_000))
        with self._lock:
            return sorted(
                self._malware_job_outbox.values(),
                key=lambda row: (row.run_after, row.job_id),
            )[:bounded]

    def acknowledge_malware_job_spec(self, job_id: str) -> None:
        with self._lock:
            if self._malware_job_outbox.pop(job_id, None) is not None:
                self._save()

    def begin_malware_scan(
        self, *, job_id: str, lease_token: str, lease_expires_at: str,
        scan_id: str, attempt_fence: str,
    ) -> DriveMalwareScan:
        if not lease_token or not attempt_fence:
            raise DriveConflictError("A live job lease and attempt fence are required.")
        live_expiry = self._require_live_malware_job(job_id, lease_token)
        if lease_expires_at and lease_expires_at != live_expiry:
            # Worker heartbeats may have extended the live lease between claim
            # and begin; the queue authority is definitive.
            lease_expires_at = live_expiry
        with self._lock:
            current = self._malware_scans.get(scan_id)
            if not current or current.job_id != job_id:
                raise DriveConflictError("Malware scan job does not match its attempt.")
            authoritative = self.get_authoritative_malware_scan(
                current.revision_id,
                account_id=current.account_id,
                space_id=current.space_id,
                policy_epoch=current.policy_epoch,
            )
            if not authoritative or authoritative.id != current.id:
                raise DriveConflictError("Malware scan attempt is no longer authoritative.")
            if current.status == "scanning" and (
                not current.lease_expires_at or current.lease_expires_at <= now_iso()
            ):
                # A newly claimed live queue lease may fence out the crashed
                # worker without waiting for a separate reconciliation pass.
                current = replace(current, status="pending")
            if current.status not in {"pending", "rescan_required"}:
                if (
                    current.status == "scanning"
                    and current.attempt_fence == attempt_fence
                    and current.lease_expires_at == lease_expires_at
                ):
                    return current
                raise DriveConflictError("Malware scan attempt cannot begin.")
            stored = replace(
                current,
                status="scanning",
                attempt_fence=attempt_fence,
                lease_expires_at=lease_expires_at,
                started_at=current.started_at or now_iso(),
                updated_at=now_iso(),
            )
            self._malware_lease_tokens[stored.id] = lease_token
            self._malware_scans[stored.id] = stored
            self._save()
            return stored

    def complete_malware_scan(
        self, *, job_id: str, lease_token: str, scan_id: str, attempt_fence: str,
        verdict: ScanVerdict, next_attempt_at: str = "", consecutive_failures: int = 0,
    ) -> DriveMalwareCompletion:
        if not lease_token or not attempt_fence:
            raise DriveConflictError("A live job lease and attempt fence are required.")
        live_expiry = self._require_live_malware_job(job_id, lease_token)
        with self._lock:
            current = self._malware_scans.get(scan_id)
            if not current or (
                current.job_id != job_id
                or current.status != "scanning"
                or current.attempt_fence != attempt_fence
            ):
                raise DriveConflictError("Malware scan lease or fence is stale.")
            if self._malware_lease_tokens.get(current.id) != lease_token:
                raise DriveConflictError("Malware scan job lease was replaced.")
            current = replace(current, lease_expires_at=live_expiry)
            authoritative = self.get_authoritative_malware_scan(
                current.revision_id,
                account_id=current.account_id,
                space_id=current.space_id,
                policy_epoch=current.policy_epoch,
            )
            if not authoritative or authoritative.id != current.id:
                raise DriveConflictError("Malware scan attempt is no longer authoritative.")

            integrity_ok = (
                verdict.sha256 == current.revision_sha256
                and verdict.size_bytes == current.revision_size_bytes
            )
            status = verdict.status if integrity_ok else "scan_error"
            threat_code = verdict.threat_code if status == "infected" else ""
            error_code = (
                verdict.error_code if status == "scan_error" and integrity_ok else
                "integrity_mismatch" if status == "scan_error" else ""
            )
            completed_at = now_iso()
            stored = replace(
                current,
                status=status,
                consecutive_failures=max(0, int(consecutive_failures)),
                next_attempt_at=next_attempt_at if status == "scan_error" else "",
                scanner_engine=verdict.engine,
                scanner_engine_version=verdict.engine_version,
                definition_version=verdict.definition_version,
                definition_timestamp=verdict.definition_timestamp,
                threat_code=threat_code,
                error_code=error_code,
                completed_at=completed_at,
                updated_at=completed_at,
            )
            validate_malware_scan(stored)
            self._malware_scans[stored.id] = stored
            self._malware_lease_tokens.pop(stored.id, None)

            file = self._files.get(current.file_id)
            ingestion_job_id = ""
            if file and file.current_revision_id == current.revision_id:
                if status == "clean" and file.desired_indexed and not file.trashed_at:
                    ingestion_job_id = drive_ingest_job_id(
                        file.id,
                        current.revision_id,
                        file.generation,
                    )
                    file = replace(file, index_status="queued", updated_at=completed_at)
                elif status != "clean" and file.desired_indexed:
                    self._delete_file_chunks(file)
                    file = replace(
                        file, active_doc_id="", index_status="blocked", updated_at=completed_at
                    )
                self._files[file.id] = file
            self._save()
            return DriveMalwareCompletion(stored, file, ingestion_job_id, True)

    def wake_retryable_malware_scans(self, *, limit: int = 100) -> int:
        """Make a bounded set of authoritative cooldown attempts retryable now."""

        bounded = max(1, min(int(limit), 1_000))
        timestamp = now_iso()
        woken = 0
        with self._lock:
            candidates: list[DriveMalwareScan] = []
            for revision in self._revisions.values():
                current = self.get_authoritative_malware_scan(
                    revision.id,
                    account_id=revision.account_id,
                    space_id=revision.space_id,
                )
                if (
                    current
                    and current.status == "scan_error"
                    and current.next_attempt_at
                    and current.next_attempt_at > timestamp
                ):
                    candidates.append(current)
            for current in sorted(
                candidates, key=lambda row: (row.next_attempt_at, row.created_at, row.id)
            )[:bounded]:
                self._malware_scans[current.id] = replace(
                    current, next_attempt_at=timestamp, updated_at=timestamp,
                )
                woken += 1
            if woken:
                self._save()
        return woken

    def reconcile_malware_scans(
        self, *, limit: int = 100,
    ) -> DriveMalwareReconcileResult:
        bounded = max(1, min(int(limit), 1_000))
        now = now_iso()
        recovered = created = enqueued = 0
        with self._lock:
            authoritative_rows: list[DriveMalwareScan] = []
            for revision in self._revisions.values():
                row = self.get_authoritative_malware_scan(
                    revision.id,
                    account_id=revision.account_id,
                    space_id=revision.space_id,
                )
                if row:
                    authoritative_rows.append(row)
            for current in sorted(
                authoritative_rows, key=lambda row: (row.next_attempt_at, row.created_at, row.id)
            )[:bounded]:
                pending_spec = self._malware_job_outbox.get(current.job_id)
                if pending_spec:
                    self._validate_malware_job_spec_identity(current, pending_spec)
                live_job = self._lookup_malware_job(current.job_id)
                if live_job:
                    self._validate_malware_job_identity(current, live_job)
                live_status = str(getattr(live_job, "status", "")) if live_job else ""
                live_running = bool(
                    live_job
                    and live_status == "running"
                    and getattr(live_job, "lease_expires_at", "") > now
                )
                if (
                    current.status in {"pending", "rescan_required"}
                    and live_status in {"failed", "succeeded"}
                ):
                    current = replace(
                        current,
                        status="scan_error",
                        error_code="job_terminal_without_verdict",
                        threat_code="",
                        completed_at=now,
                        next_attempt_at=now,
                        updated_at=now,
                    )
                    self._malware_scans[current.id] = current
                    recovered += 1
                elif current.status == "scanning" and live_running:
                    current = replace(
                        current,
                        lease_expires_at=getattr(live_job, "lease_expires_at"),
                        updated_at=now,
                    )
                    self._malware_scans[current.id] = current
                elif (
                    current.status == "scanning"
                    and (not current.lease_expires_at or current.lease_expires_at <= now)
                ):
                    current = replace(
                        current,
                        status="scan_error",
                        error_code="lease_expired",
                        threat_code="",
                        completed_at=now,
                        next_attempt_at=now,
                        updated_at=now,
                    )
                    self._malware_scans[current.id] = current
                    recovered += 1
                if current.status == "scan_error" and (
                    not current.next_attempt_at or current.next_attempt_at <= now
                ):
                    sequence = current.attempt_sequence + 1
                    scan_id = self._deterministic_scan_id(
                        current.revision_id, current.policy_epoch, sequence
                    )
                    job_id = self._deterministic_job_id(f"drive-malware:{scan_id}")
                    current = replace(
                        current,
                        id=scan_id,
                        status="pending",
                        origin="rescan" if current.origin != "legacy_backfill" else current.origin,
                        attempt_sequence=sequence,
                        job_id=job_id,
                        next_attempt_at="",
                        attempt_fence="",
                        lease_expires_at="",
                        scanner_engine="",
                        scanner_engine_version="",
                        definition_version="",
                        definition_timestamp="",
                        threat_code="",
                        error_code="",
                        started_at="",
                        completed_at="",
                        created_at=now,
                        updated_at=now,
                    )
                    self._malware_scans[current.id] = current
                    created += 1
                if current.status in {"pending", "rescan_required"}:
                    if current.origin == "legacy_backfill" and any(
                        row.origin == "legacy_backfill"
                        and row.job_id != current.job_id
                        and row.status in {"pending", "scanning"}
                        and (
                            row.job_id in self._malware_job_outbox
                            or self._malware_job_is_active(row.job_id)
                        )
                        for row in self._malware_scans.values()
                    ):
                        continue
                    if current.job_id not in self._malware_job_outbox:
                        self._record_malware_job_spec(DriveMalwareJobSpec(
                            job_id=current.job_id,
                            tenant_id=current.tenant_id,
                            account_id=current.account_id,
                            space_id=current.space_id,
                            scan_id=current.id,
                            revision_id=current.revision_id,
                            origin=current.origin,
                            requested_by="malware-reconciler",
                            max_attempts=5,
                            run_after=current.next_attempt_at or now,
                            idempotency_key=f"drive-malware:{current.id}",
                        ))
                        enqueued += 1
            if recovered or created or enqueued:
                self._save()
        return DriveMalwareReconcileResult(recovered, created, enqueued)

    def request_malware_rescan(
        self, *, file_id: str, account_id: str, space_id: str,
        expected_generation: int, requested_by: str, scan_id: str, scan_job_id: str,
        idempotency_key: str = "",
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH, scan_job_max_attempts: int = 5,
    ) -> tuple[DriveFile, DriveMalwareScan]:
        with self._lock:
            if policy_epoch != DRIVE_MALWARE_POLICY_EPOCH:
                raise DriveConflictError("Malware policy epoch is not active.")
            file = self.get_file(file_id, account_id=account_id, space_id=space_id)
            if not file:
                raise DriveGenerationConflict("Drive file changed before malware rescan.")
            existing = self._malware_scans.get(scan_id)
            if existing:
                if not (
                    existing.file_id == file.id
                    and existing.revision_id == file.current_revision_id
                    and existing.account_id == account_id
                    and existing.space_id == space_id
                    and existing.policy_epoch == policy_epoch
                    and existing.origin == "rescan"
                    and existing.job_id == scan_job_id
                ):
                    raise DriveConflictError("Malware rescan idempotency identity conflicts.")
                self._verify_existing_malware_job_spec(DriveMalwareJobSpec(
                    job_id=scan_job_id,
                    tenant_id=existing.tenant_id,
                    account_id=existing.account_id,
                    space_id=existing.space_id,
                    scan_id=existing.id,
                    revision_id=existing.revision_id,
                    origin=existing.origin,
                    requested_by=requested_by,
                    max_attempts=scan_job_max_attempts,
                    run_after=existing.next_attempt_at or existing.created_at,
                    idempotency_key=f"drive-malware:{existing.id}",
                ))
                return file, existing
            if file.generation != expected_generation:
                raise DriveGenerationConflict("Drive file changed before malware rescan.")
            if scan_job_id in self._malware_job_outbox:
                raise DriveConflictError("Malware scan job identity already exists.")
            revision = self.get_revision(
                file.current_revision_id, account_id=account_id, space_id=space_id
            )
            if not revision:
                raise DriveConflictError("Drive current revision is missing.")
            current = self.get_authoritative_malware_scan(
                revision.id,
                account_id=account_id,
                space_id=space_id,
                policy_epoch=policy_epoch,
            )
            sequence = (current.attempt_sequence if current else 0) + 1
            scan = DriveMalwareScan(
                id=scan_id,
                tenant_id=revision.tenant_id,
                account_id=account_id,
                space_id=space_id,
                file_id=file.id,
                revision_id=revision.id,
                revision_sha256=revision.sha256,
                revision_size_bytes=revision.size_bytes,
                policy_epoch=policy_epoch,
                status="pending",
                origin="rescan",
                attempt_sequence=sequence,
                job_id=scan_job_id,
            )
            validate_malware_scan(scan)
            if scan.id in self._malware_scans or any(
                row.revision_id == revision.id
                and row.policy_epoch == policy_epoch
                and row.attempt_sequence == sequence
                for row in self._malware_scans.values()
            ):
                raise DriveConflictError("Malware rescan attempt already exists.")
            # Fail closed first. A later metadata/outbox failure may cause extra
            # unavailability, but can never leave old chunks authoritative.
            self._delete_file_chunks(file)
            timestamp = now_iso()
            stored_file = replace(
                file,
                active_doc_id="",
                index_status="awaiting_scan" if file.desired_indexed else "not_indexed",
                generation=file.generation + 1,
                updated_at=timestamp,
            )
            stored_scan = replace(scan, created_at=timestamp, updated_at=timestamp)
            self._files[file.id] = stored_file
            self._malware_scans[scan.id] = stored_scan
            self._record_malware_job_spec(DriveMalwareJobSpec(
                job_id=scan_job_id,
                tenant_id=scan.tenant_id,
                account_id=scan.account_id,
                space_id=scan.space_id,
                scan_id=scan.id,
                revision_id=scan.revision_id,
                origin=scan.origin,
                requested_by=requested_by,
                max_attempts=scan_job_max_attempts,
                run_after=timestamp,
                idempotency_key=f"drive-malware:{scan.id}",
            ))
            self._save()
            return stored_file, stored_scan

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
            revision = self.get_revision(
                revision_id, account_id=account_id, space_id=space_id
            )
            scan = self.get_authoritative_malware_scan(
                revision_id, account_id=account_id, space_id=space_id
            )
            if not revision or not is_clean_attestation(revision, scan):
                raise DriveGenerationConflict("Drive revision has no current clean attestation.")
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
            scan_ids = [
                key for key, row in self._malware_scans.items() if row.revision_id in revision_ids
            ]
            scan_job_ids = {self._malware_scans[key].job_id for key in scan_ids}
            for key in scan_ids:
                self._malware_scans.pop(key, None)
                self._malware_lease_tokens.pop(key, None)
            for key in scan_job_ids:
                self._malware_job_outbox.pop(key, None)
            for key in revision_ids:
                self._revisions.pop(key, None)
            self._files.pop(file.id, None)
            self._save()
            return {
                "files": 1, "revisions": len(revision_ids),
                "malware_scans": len(scan_ids), "chunks": chunks,
            }

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
            "malware_scans": [
                asdict(row) for row in self._malware_scans.values() if match(row)
            ],
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
                "malware_scans": sum(1 for row in self._malware_scans.values() if match(row)),
            }
            self._folders = {key: row for key, row in self._folders.items() if not match(row)}
            self._files = {key: row for key, row in self._files.items() if not match(row)}
            self._revisions = {key: row for key, row in self._revisions.items() if not match(row)}
            self._uploads = {key: row for key, row in self._uploads.items() if not match(row)}
            removed_job_ids = {
                row.job_id for row in self._malware_scans.values() if match(row)
            }
            self._malware_scans = {
                key: row for key, row in self._malware_scans.items() if not match(row)
            }
            self._malware_lease_tokens = {
                key: token for key, token in self._malware_lease_tokens.items()
                if key in self._malware_scans
            }
            self._malware_job_outbox = {
                key: row for key, row in self._malware_job_outbox.items()
                if key not in removed_job_ids
            }
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

    def _record_malware_job_spec(self, spec: DriveMalwareJobSpec) -> None:
        """Insert an outbox row without allowing an identity collision to mutate it."""

        existing = self._malware_job_outbox.get(spec.job_id)
        if existing and existing != spec:
            raise DriveConflictError("Malware scan job identity already exists.")
        self._malware_job_outbox[spec.job_id] = existing or spec

    @staticmethod
    def _validate_malware_job_spec_identity(
        scan: DriveMalwareScan, spec: DriveMalwareJobSpec,
    ) -> None:
        if not (
            spec.job_id == scan.job_id
            and spec.tenant_id == scan.tenant_id
            and spec.account_id == scan.account_id
            and spec.space_id == scan.space_id
            and spec.scan_id == scan.id
            and spec.revision_id == scan.revision_id
            and spec.origin == scan.origin
            and spec.idempotency_key == f"drive-malware:{scan.id}"
        ):
            raise DriveConflictError("Malware scan job identity already exists.")

    @staticmethod
    def _validate_malware_job_identity(scan: DriveMalwareScan, job) -> None:
        expected_payload = {
            "scan_id": scan.id,
            "revision_id": scan.revision_id,
            "origin": scan.origin,
        }
        if not (
            getattr(job, "id", "") == scan.job_id
            and getattr(job, "type", "") == "drive_revision_malware_scan"
            and getattr(job, "tenant_id", "") == scan.tenant_id
            and getattr(job, "account_id", "") == scan.account_id
            and getattr(job, "space_id", "") == scan.space_id
            and getattr(job, "payload", None) == expected_payload
        ):
            raise DriveConflictError("Malware scan job identity already exists.")

    def _verify_existing_malware_job_spec(self, spec: DriveMalwareJobSpec) -> None:
        pending = self._malware_job_outbox.get(spec.job_id)
        if pending:
            if pending != spec:
                raise DriveConflictError("Malware scan job identity already exists.")
            return
        job = self._lookup_malware_job(spec.job_id)
        expected_payload = {
            "scan_id": spec.scan_id,
            "revision_id": spec.revision_id,
            "origin": spec.origin,
        }
        if not job or not (
            getattr(job, "id", "") == spec.job_id
            and getattr(job, "type", "") == "drive_revision_malware_scan"
            and getattr(job, "tenant_id", "") == spec.tenant_id
            and getattr(job, "account_id", "") == spec.account_id
            and getattr(job, "space_id", "") == spec.space_id
            and getattr(job, "requested_by", "") == spec.requested_by
            and getattr(job, "payload", None) == expected_payload
            and int(getattr(job, "max_attempts", 0)) == spec.max_attempts
        ):
            raise DriveConflictError("Malware scan job identity already exists.")

    @staticmethod
    def _same_malware_attempt_identity(
        left: DriveMalwareScan, right: DriveMalwareScan,
    ) -> bool:
        return (
            left.id, left.tenant_id, left.account_id, left.space_id, left.file_id,
            left.revision_id, left.revision_sha256, left.revision_size_bytes,
            left.policy_epoch, left.origin, left.attempt_sequence, left.job_id,
        ) == (
            right.id, right.tenant_id, right.account_id, right.space_id, right.file_id,
            right.revision_id, right.revision_sha256, right.revision_size_bytes,
            right.policy_epoch, right.origin, right.attempt_sequence, right.job_id,
        )

    @staticmethod
    def _deterministic_scan_id(revision_id: str, policy_epoch: int, sequence: int) -> str:
        value = uuid5(
            NAMESPACE_URL, f"onebrain:drive-malware:{revision_id}:{policy_epoch}:{sequence}"
        )
        return f"scan_{value.hex}"

    @staticmethod
    def _deterministic_job_id(key: str) -> str:
        return f"job_{uuid5(NAMESPACE_URL, f'onebrain:{key}').hex}"

    def _lookup_malware_job(self, job_id: str):
        if not self._malware_job_lookup:
            return None
        return self._malware_job_lookup(job_id)

    def _require_live_malware_job(self, job_id: str, lease_token: str) -> str:
        if not self._malware_job_lookup:
            raise DriveConflictError("Memory malware queue authority is not bound.")
        job = self._lookup_malware_job(job_id)
        expiry = getattr(job, "lease_expires_at", "") if job else ""
        try:
            live = (
                bool(job)
                and getattr(job, "status", "") == "running"
                and getattr(job, "lease_token", "") == lease_token
                and datetime.fromisoformat(expiry) > datetime.now(timezone.utc)
            )
        except (TypeError, ValueError):
            live = False
        if not live:
            raise DriveConflictError("Malware scan job lease is no longer active.")
        return expiry

    def _malware_job_is_active(self, job_id: str) -> bool:
        job = self._lookup_malware_job(job_id)
        if not job:
            return False
        return getattr(job, "status", "") in {"queued", "retrying", "running"}

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

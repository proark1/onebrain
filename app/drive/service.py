"""Drive lifecycle orchestration independent from HTTP and storage implementations."""

from __future__ import annotations

import mimetypes
import uuid
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.drive.access import (
    authorize_drive_space,
    list_drive_roots,
    require_file_access,
    require_folder_access,
)
from app.drive.base import (
    DriveConflictError,
    DriveEntryPage,
    DriveFile,
    DriveFolder,
    DriveGenerationConflict,
    DriveLimitError,
    DriveRevision,
    DriveUploadSession,
    normalize_name,
    now_iso,
)
from app.drive.blobs import drive_scope_prefix, drive_storage_key
from app.jobs.base import JOB_DRIVE_FILE_INGEST
from app.platform.base import AuditEvent, DataAccessEvent, Tombstone, target_is_held
from app.security.policy import GENERAL_CATEGORY, GLOBAL_LOCATION, Classification


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class DriveService:
    def __init__(self, *, store, blobs, platform_store, job_store, settings):
        self.store = store
        self.blobs = blobs
        self.platform_store = platform_store
        self.job_store = job_store
        self.settings = settings

    def roots(self, principal):
        roots = list_drive_roots(principal, self.platform_store)
        if not getattr(self.settings, "drive_private_spaces_enabled", False):
            roots = [row for row in roots if row.kind != "personal"]
        return roots

    def authorize_space(self, principal, account_id: str, space_id: str):
        space, owner_user_id = authorize_drive_space(
            principal, account_id, space_id, self.platform_store,
        )
        if owner_user_id and not getattr(self.settings, "drive_private_spaces_enabled", False):
            raise HTTPException(status_code=404, detail="Drive space not found.")
        return space, owner_user_id

    def list_entries(
        self,
        principal,
        *,
        account_id: str,
        space_id: str,
        folder_id: str = "",
        query: str = "",
        trashed: bool = False,
        cursor: str = "",
        limit: int = 100,
    ) -> DriveEntryPage:
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        if folder_id:
            self._folder_for_principal(
                principal, account_id, space_id, folder_id,
                space_kind=space.kind, owner_user_id=owner_user_id,
            )
        page = self.store.list_entries(
            account_id=account_id,
            space_id=space_id,
            folder_id=folder_id,
            query=query,
            trashed=trashed,
            cursor=cursor,
            limit=limit,
        )
        authorized_files = tuple(row for row in page.files if self._can_access(principal, row))
        self._reconcile_index_jobs(authorized_files)
        return DriveEntryPage(
            folders=tuple(
                row for row in page.folders
                if self._can_access_folder(
                    principal, row, space_kind=space.kind, owner_user_id=owner_user_id,
                )
            ),
            files=authorized_files,
            next_cursor=page.next_cursor,
        )

    def breadcrumbs(self, principal, *, account_id: str, space_id: str, folder_id: str):
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        rows = self.store.breadcrumbs(folder_id, account_id=account_id, space_id=space_id)
        for row in rows:
            require_folder_access(
                principal, row, space_kind=space.kind, owner_user_id=owner_user_id,
            )
        return rows

    def create_folder(
        self,
        principal,
        *,
        account_id: str,
        space_id: str,
        parent_id: str,
        name: str,
        classification: str = "",
        location: str = "",
        category: str = "",
        index_for_ai: bool | None = None,
        idempotency_key: str = "",
    ) -> DriveFolder:
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        parent = self._folder_for_principal(
            principal, account_id, space_id, parent_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        ) if parent_id else None
        effective = self._effective_policy(
            principal, account_id=account_id, space_id=space_id, folder=parent,
            classification=classification, location=location, category=category,
            index_for_ai=index_for_ai,
        )
        key = (idempotency_key or "").strip()
        if key and len(key) > 128:
            raise ValueError("Folder idempotency key must be at most 128 characters.")
        folder_id = (
            "fld_" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"onebrain:drive-folder:{principal.tenant_id}:{space_id}:{principal.user_id}:{key}",
            ).hex
            if key else _id("fld")
        )
        folder = self.store.create_folder(DriveFolder(
            id=folder_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            parent_id=parent_id,
            name=normalize_name(name),
            default_classification=effective[0],
            default_location=effective[1],
            default_category=effective[2],
            default_indexed=effective[3],
            created_by=principal.user_id,
        ))
        self._audit(principal, "drive.folder.created", "drive_folder", folder.id, folder.space_id)
        return folder

    def update_folder_defaults(
        self,
        principal,
        *,
        account_id: str,
        space_id: str,
        folder_id: str,
        generation: int,
        name: str | None = None,
        classification: str | None = None,
        location: str | None = None,
        category: str | None = None,
        index_for_ai: bool | None = None,
        confirm_audience_change: bool = False,
    ) -> DriveFolder:
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        folder = self._folder_for_principal(
            principal, account_id, space_id, folder_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        )
        if folder.generation != generation:
            raise DriveGenerationConflict("Folder changed; refresh and try again.")
        parent = self._folder_for_principal(
            principal, account_id, space_id, folder.parent_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        ) if folder.parent_id else None
        effective = self._effective_policy(
            principal,
            account_id=account_id,
            space_id=space_id,
            folder=parent,
            classification=folder.default_classification if classification is None else classification,
            location=folder.default_location if location is None else location,
            category=folder.default_category if category is None else category,
            index_for_ai=folder.default_indexed if index_for_ai is None else index_for_ai,
        )
        if self._policy_widens(
            (folder.default_classification, folder.default_location,
             folder.default_category, folder.default_indexed),
            effective,
        ) and (principal.role_id != "admin" or not confirm_audience_change):
            raise PermissionError(
                "Widening a folder audience requires an administrator and explicit confirmation."
            )
        updated = replace(
            folder,
            name=normalize_name(name) if name is not None else folder.name,
            default_classification=effective[0],
            default_location=effective[1],
            default_category=effective[2],
            default_indexed=effective[3],
            generation=folder.generation + 1,
        )
        if updated == folder:
            return folder
        stored = self.store.update_folder(updated, expected_generation=folder.generation)
        self._audit(
            principal, "drive.folder.defaults_changed", "drive_folder", folder.id, space_id,
            {"generation": stored.generation},
        )
        return stored

    def update_file(
        self,
        principal,
        *,
        account_id: str,
        space_id: str,
        file_id: str,
        generation: int,
        folder_id: str | None = None,
        name: str | None = None,
        classification: str | None = None,
        location: str | None = None,
        category: str | None = None,
        index_for_ai: bool | None = None,
        confirm_audience_change: bool = False,
    ) -> DriveFile:
        file = self.get_file(
            principal, account_id=account_id, space_id=space_id, file_id=file_id,
        )
        if file.generation != generation:
            raise DriveGenerationConflict("File changed; refresh and try again.")
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        target_folder_id = file.folder_id if folder_id is None else folder_id
        target_folder = self._folder_for_principal(
            principal, account_id, space_id, target_folder_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        ) if target_folder_id else None
        moved = target_folder_id != file.folder_id
        effective = self._effective_policy(
            principal,
            account_id=account_id,
            space_id=space_id,
            folder=target_folder,
            classification=(
                target_folder.default_classification if moved and classification is None and target_folder
                else file.classification if classification is None else classification
            ),
            location=(
                target_folder.default_location if moved and location is None and target_folder
                else file.location if location is None else location
            ),
            category=(
                target_folder.default_category if moved and category is None and target_folder
                else file.category if category is None else category
            ),
            index_for_ai=(
                target_folder.default_indexed if moved and index_for_ai is None and target_folder
                else file.desired_indexed if index_for_ai is None else index_for_ai
            ),
        )
        previous_policy = (file.classification, file.location, file.category, file.desired_indexed)
        if self._policy_widens(previous_policy, effective) and (
            principal.role_id != "admin" or not confirm_audience_change
        ):
            raise PermissionError(
                "Widening a file audience requires an administrator and explicit confirmation."
            )
        next_name = normalize_name(name) if name is not None else file.name
        projection_changed = (
            next_name != file.name or target_folder_id != file.folder_id
            or previous_policy != effective
        )
        if not projection_changed:
            return file
        proposed = replace(
            file,
            folder_id=target_folder_id,
            name=next_name,
            classification=effective[0],
            location=effective[1],
            category=effective[2],
            desired_indexed=effective[3],
            active_doc_id="",
            index_status="queued" if effective[3] and not file.trashed_at else "not_indexed",
            generation=file.generation + 1,
        )
        stored = self.store.update_file(proposed, expected_generation=file.generation)
        if stored.desired_indexed and not stored.trashed_at:
            self._enqueue_index(stored)
        self._audit(
            principal, "drive.file.changed", "drive_file", file.id, space_id,
            {"generation": stored.generation, "moved": moved, "reindexed": stored.desired_indexed},
        )
        return stored

    def create_upload(
        self,
        principal,
        *,
        account_id: str,
        space_id: str,
        folder_id: str,
        name: str,
        size_bytes: int,
        index_for_ai: bool | None,
        idempotency_key: str,
        classification: str = "",
        location: str = "",
        category: str = "",
    ) -> DriveUploadSession:
        if self._drive_mode() == "disabled":
            raise PermissionError("Drive storage is disabled by deployment privacy policy.")
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        self.cleanup_expired_uploads(tenant_id=principal.tenant_id, account_id=account_id)
        name = normalize_name(name)
        size_bytes = int(size_bytes)
        if size_bytes <= 0 or size_bytes > self.settings.drive_max_file_bytes:
            raise ValueError(f"Drive files must be between 1 and {self.settings.drive_max_file_bytes} bytes.")
        folder = self._folder_for_principal(
            principal, account_id, space_id, folder_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        ) if folder_id else None
        effective = self._effective_policy(
            principal, account_id=account_id, space_id=space_id, folder=folder,
            classification=classification, location=location, category=category,
            index_for_ai=index_for_ai,
        )
        self.blobs.ensure_capacity(size_bytes)
        upload_id = _id("upl")
        upload = DriveUploadSession(
            id=upload_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            folder_id=folder_id,
            name=name,
            size_bytes=size_bytes,
            desired_indexed=effective[3],
            classification=effective[0],
            location=effective[1],
            category=effective[2],
            created_by=principal.user_id,
            idempotency_key=(idempotency_key or "").strip(),
            staging_key=f"staging/{upload_id}",
            expires_at=(datetime.now(timezone.utc) + timedelta(
                seconds=max(60, int(self.settings.drive_upload_session_seconds)),
            )).isoformat(),
        )
        stored = self.store.create_upload(upload)
        if stored.id != upload.id and not self._same_upload_request(stored, upload):
            raise DriveConflictError(
                "This upload idempotency key was already used with different file parameters."
            )
        self._audit(principal, "drive.upload.created", "drive_upload", stored.id, space_id)
        return stored

    def cleanup_expired_uploads(self, *, tenant_id: str, account_id: str, limit: int = 500) -> int:
        """Bound abandoned staging data whenever an account initiates new work.

        The metadata query is bounded and may be called repeatedly. Each row is
        marked expired only after its staging bytes are gone, so filesystem
        failures remain visible and retryable instead of silently leaking data.
        """

        cleaned = 0
        for upload in self.store.list_expired_uploads(
            tenant_id=tenant_id,
            account_id=account_id,
            before=now_iso(),
            limit=limit,
        ):
            self.blobs.delete_staging(upload.id)
            self.store.update_upload(replace(
                upload,
                status="expired",
                error="Upload session expired.",
            ))
            cleaned += 1
        return cleaned

    def begin_upload(self, principal, upload_id: str):
        upload = self._upload_for_principal(principal, upload_id)
        if upload.status == "uploaded":
            return upload, None
        if upload.status not in {"created", "uploading"}:
            raise ValueError("This upload session cannot accept content.")
        existing = self.blobs.staging_info(upload.id)
        if existing:
            if existing.size_bytes == upload.size_bytes:
                recovered = self.store.update_upload(replace(
                    upload,
                    status="uploaded",
                    bytes_received=existing.size_bytes,
                    sha256=existing.sha256,
                    media_type=upload.media_type or mimetypes.guess_type(upload.name)[0]
                    or "application/octet-stream",
                    error="",
                ))
                return recovered, None
            self.blobs.delete_staging(upload.id)
        self.blobs.ensure_capacity(upload.size_bytes)
        writer = self.blobs.begin_staging(
            upload.id,
            max_bytes=min(self.settings.drive_max_file_bytes, upload.size_bytes),
        )
        try:
            upload = self.store.update_upload(replace(upload, status="uploading", error=""))
        except Exception:
            writer.abort()
            raise
        return upload, writer

    def finish_upload_content(self, principal, upload: DriveUploadSession, info, media_type: str = ""):
        if info.size_bytes != upload.size_bytes:
            self.blobs.delete_staging(upload.id)
            failed = self.store.update_upload(replace(
                upload,
                status="failed",
                bytes_received=info.size_bytes,
                sha256=info.sha256,
                error="Uploaded byte count does not match the declared file size.",
            ))
            raise ValueError(failed.error)
        detected = (media_type or mimetypes.guess_type(upload.name)[0] or "application/octet-stream").split(";", 1)[0]
        return self.store.update_upload(replace(
            upload,
            status="uploaded",
            bytes_received=info.size_bytes,
            sha256=info.sha256,
            media_type=detected,
            error="",
        ))

    def complete_upload(self, principal, upload_id: str) -> tuple[DriveUploadSession, DriveFile]:
        upload = self._upload_for_principal(principal, upload_id)
        if upload.status == "completed" and upload.file_id:
            existing = self.store.get_file(upload.file_id, account_id=upload.account_id, space_id=upload.space_id)
            if not existing:
                raise RuntimeError("Completed Drive upload is missing its file metadata.")
            return upload, existing
        if upload.status not in {"uploaded", "completing"}:
            raise ValueError("Upload content must finish before completion.")
        file_id = upload.file_id or self._deterministic_id("fil", upload.id)
        revision_id = upload.revision_id or self._deterministic_id("rev", upload.id)
        upload = self.store.update_upload(replace(
            upload,
            status="completing",
            file_id=file_id,
            revision_id=revision_id,
            error="",
        ))
        space, owner_user_id = self.authorize_space(principal, upload.account_id, upload.space_id)
        if upload.folder_id:
            self._folder_for_principal(
                principal, upload.account_id, upload.space_id, upload.folder_id,
                space_kind=space.kind, owner_user_id=owner_user_id,
            )
        permanent_key = drive_storage_key(
            upload.tenant_id, upload.account_id, upload.space_id, file_id, revision_id,
        )
        info = self.blobs.promote(upload.id, permanent_key)
        if info.size_bytes != upload.size_bytes or info.sha256 != upload.sha256:
            self.blobs.delete(permanent_key)
            raise RuntimeError("Promoted Drive blob does not match the completed upload.")
        file = self.store.get_file(file_id, account_id=upload.account_id, space_id=upload.space_id)
        if not file:
            file = self.store.create_file(DriveFile(
                id=file_id,
                tenant_id=upload.tenant_id,
                account_id=upload.account_id,
                space_id=upload.space_id,
                folder_id=upload.folder_id,
                name=upload.name,
                classification=upload.classification,
                location=upload.location,
                category=upload.category,
                space_kind=space.kind,
                owner_user_id=owner_user_id,
                desired_indexed=upload.desired_indexed,
                approval_status="not_required",
                index_status="queued" if upload.desired_indexed else "not_indexed",
                current_revision_id=revision_id,
                generation=1,
                uploaded_by=principal.user_id,
            ))
        revision = self.store.create_revision(DriveRevision(
            id=revision_id,
            tenant_id=upload.tenant_id,
            account_id=upload.account_id,
            space_id=upload.space_id,
            file_id=file.id,
            upload_session_id=upload.id,
            storage_key=permanent_key,
            sha256=upload.sha256,
            size_bytes=upload.size_bytes,
            media_type=upload.media_type,
            original_name=upload.name,
            created_by=principal.user_id,
        ))
        if file.current_revision_id != revision.id:
            file = self.store.update_file(
                replace(
                    file,
                    current_revision_id=revision.id,
                    generation=file.generation + 1,
                    active_doc_id="",
                    index_status="queued" if file.desired_indexed else "not_indexed",
                ),
                expected_generation=file.generation,
            )
        if file.desired_indexed:
            self._enqueue_index(file)
        upload = self.store.update_upload(replace(
            upload,
            status="completed",
            file_id=file.id,
            revision_id=revision.id,
            error="",
        ))
        self._audit(principal, "drive.file.created", "drive_file", file.id, file.space_id, {
            "generation": file.generation,
            "index_requested": file.desired_indexed,
        })
        return upload, file

    def get_file(self, principal, *, account_id: str, space_id: str, file_id: str) -> DriveFile:
        self.authorize_space(principal, account_id, space_id)
        file = self.store.get_file(file_id, account_id=account_id, space_id=space_id)
        if not file:
            raise KeyError("File not found.")
        require_file_access(principal, file)
        return file

    def get_revision_for_download(self, principal, *, account_id: str, space_id: str, file_id: str):
        file = self.get_file(principal, account_id=account_id, space_id=space_id, file_id=file_id)
        revision = self.store.get_revision(
            file.current_revision_id, account_id=account_id, space_id=space_id,
        )
        if not revision:
            raise RuntimeError("Drive original is unavailable.")
        self.platform_store.record_data_access(DataAccessEvent(
            id=_id("dae"), account_id=account_id, space_id=space_id,
            actor_id=principal.user_id, actor_type=principal.principal_type,
            action="drive.original.download", target_type="drive_file", target_id=file.id,
            app_id="onebrain_core", purpose="knowledge_management", decision="allowed",
            meta={"revision_id": revision.id, "size_bytes": revision.size_bytes},
        ))
        return file, revision

    def trash_file(self, principal, *, account_id: str, space_id: str, file_id: str, generation: int) -> DriveFile:
        file = self.get_file(principal, account_id=account_id, space_id=space_id, file_id=file_id)
        if file.generation != generation:
            raise DriveGenerationConflict("File changed; refresh and try again.")
        if file.trashed_at:
            return file
        stored = self.store.update_file(replace(
            file,
            original_folder_id=file.folder_id,
            trashed_at=now_iso(),
            trash_operation_id="",
            active_doc_id="",
            index_status="not_indexed",
            generation=file.generation + 1,
        ), expected_generation=file.generation)
        self._audit(principal, "drive.file.trashed", "drive_file", file.id, space_id)
        return stored

    def restore_file(self, principal, *, account_id: str, space_id: str, file_id: str, generation: int) -> DriveFile:
        file = self.get_file(principal, account_id=account_id, space_id=space_id, file_id=file_id)
        if file.generation != generation:
            raise DriveGenerationConflict("File changed; refresh and try again.")
        if not file.trashed_at:
            if file.desired_indexed and file.index_status in {"queued", "failed", "stale"}:
                self._enqueue_index(file)
            return file
        if file.trash_operation_id:
            raise DriveConflictError("Restore this file through its parent folder.")
        folder_id = file.original_folder_id
        if folder_id:
            try:
                space, owner_user_id = self.authorize_space(principal, account_id, space_id)
                self._folder_for_principal(
                    principal, account_id, space_id, folder_id,
                    space_kind=space.kind, owner_user_id=owner_user_id,
                )
            except (KeyError, HTTPException):
                folder_id = ""
        proposed = replace(
            file,
            folder_id=folder_id,
            trashed_at="",
            original_folder_id="",
            trash_operation_id="",
            generation=file.generation + 1,
            index_status=(
                "queued" if file.desired_indexed and self._indexing_allowed() else "not_indexed"
            ),
        )
        stored = self.store.update_file(proposed, expected_generation=file.generation)
        if stored.desired_indexed and self._indexing_allowed():
            self._enqueue_index(stored)
        self._audit(principal, "drive.file.restored", "drive_file", file.id, space_id)
        return stored

    def trash_folder(
        self, principal, *, account_id: str, space_id: str, folder_id: str, generation: int,
    ) -> DriveFolder:
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        folder = self._folder_for_principal(
            principal, account_id, space_id, folder_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        )
        if folder.generation != generation:
            raise DriveGenerationConflict("Folder changed; refresh and try again.")
        if folder.trashed_at:
            return folder
        folders, files = self._collect_tree_snapshot(folder, trashed=False)
        self._require_tree_access(
            principal, folders, files, space_kind=space.kind, owner_user_id=owner_user_id,
        )
        timestamp = now_iso()
        operation_id = _id("trsh")
        result = self.store.trash_folder_tree(
            root=folder,
            expected_generation=folder.generation,
            operation_id=operation_id,
            timestamp=timestamp,
            folder_generations={row.id: row.generation for row in folders},
            file_generations={row.id: row.generation for row in files},
        )
        self._audit(principal, "drive.folder.trashed", "drive_folder", folder.id, space_id)
        return result.root

    def restore_folder(
        self, principal, *, account_id: str, space_id: str, folder_id: str, generation: int,
    ) -> DriveFolder:
        space, owner_user_id = self.authorize_space(principal, account_id, space_id)
        folder = self._folder_for_principal(
            principal, account_id, space_id, folder_id,
            space_kind=space.kind, owner_user_id=owner_user_id,
        )
        if folder.generation != generation:
            raise DriveGenerationConflict("Folder changed; refresh and try again.")
        if not folder.trashed_at:
            return folder
        if not folder.trash_operation_id:
            raise DriveConflictError("This folder has no restorable trash operation.")
        parent_id = folder.original_parent_id
        if parent_id:
            try:
                self._folder_for_principal(
                    principal, account_id, space_id, parent_id,
                    space_kind=space.kind, owner_user_id=owner_user_id,
                )
            except (KeyError, HTTPException):
                parent_id = ""
        folders, files = self._collect_tree_snapshot(
            folder, trashed=True, operation_id=folder.trash_operation_id,
        )
        self._require_tree_access(
            principal, folders, files, space_kind=space.kind, owner_user_id=owner_user_id,
        )
        result = self.store.restore_folder_tree(
            root=replace(folder, parent_id=parent_id),
            expected_generation=folder.generation,
            operation_id=folder.trash_operation_id,
            folder_generations={row.id: row.generation for row in folders},
            file_generations={row.id: row.generation for row in files},
            indexing_enabled=self._indexing_allowed(),
        )
        for restored_file in result.files:
            if restored_file.desired_indexed and restored_file.index_status == "queued":
                self._enqueue_index(restored_file)
        self._audit(principal, "drive.folder.restored", "drive_folder", folder.id, space_id)
        return result.root

    def set_indexing(
        self, principal, *, account_id: str, space_id: str, file_id: str,
        generation: int, enabled: bool,
    ) -> DriveFile:
        file = self.get_file(principal, account_id=account_id, space_id=space_id, file_id=file_id)
        if file.generation != generation:
            raise DriveGenerationConflict("File changed; refresh and try again.")
        if enabled and self._drive_mode() != "storage_and_indexing":
            raise PermissionError("AI indexing is disabled by deployment privacy policy.")
        if enabled and file.folder_id:
            space, owner_user_id = self.authorize_space(principal, account_id, space_id)
            folder = self._folder_for_principal(
                principal,
                account_id,
                space_id,
                file.folder_id,
                space_kind=space.kind,
                owner_user_id=owner_user_id,
            )
            # A per-file toggle is still a child filing-policy mutation. Route it
            # through the same narrow-only validator used by upload and move so
            # it cannot override a parent folder configured as "Never index".
            self._effective_policy(
                principal,
                account_id=account_id,
                space_id=space_id,
                folder=folder,
                classification=file.classification,
                location=file.location,
                category=file.category,
                index_for_ai=True,
            )
        proposed = replace(
            file,
            desired_indexed=bool(enabled),
            active_doc_id=file.active_doc_id if enabled else "",
            index_status="queued" if enabled else "not_indexed",
            generation=file.generation + 1,
        )
        stored = self.store.update_file(proposed, expected_generation=file.generation)
        if enabled and not stored.trashed_at:
            self._enqueue_index(stored)
        self._audit(principal, "drive.file.indexing_changed", "drive_file", file.id, space_id, {
            "enabled": bool(enabled), "generation": stored.generation,
        })
        return stored

    def approve_file(
        self, principal, *, account_id: str, space_id: str, file_id: str, generation: int,
    ) -> DriveFile:
        file = self.get_file(principal, account_id=account_id, space_id=space_id, file_id=file_id)
        if file.uploaded_by == principal.user_id:
            raise PermissionError("You cannot approve a file you uploaded.")
        if file.approval_status == "approved" and file.index_status in {"queued", "failed", "stale"}:
            self._enqueue_index(file)
            return file
        if file.generation != generation or file.approval_status != "pending":
            raise DriveGenerationConflict("File is no longer awaiting this approval.")
        proposed = replace(
            file,
            approval_status="approved",
            approved_by=principal.user_id,
            index_status="queued",
            generation=file.generation + 1,
        )
        stored = self.store.update_file(proposed, expected_generation=file.generation)
        self._enqueue_index(stored)
        self._audit(principal, "drive.file.approved", "drive_file", file.id, space_id)
        return stored

    def permanently_delete_file(
        self, principal, *, account_id: str, space_id: str, file_id: str, generation: int,
        reason: str = "",
    ) -> dict[str, int]:
        if principal.role_id != "admin":
            raise PermissionError("Only an account administrator can permanently delete Drive files.")
        # The same account-wide guard is acquired by legal-hold creation. Keep it
        # across the final hold check, structural unpublish, blob removal, and
        # metadata deletion so a newly-created hold can never race this operation.
        guard_factory = getattr(self.platform_store, "deletion_guard", None)
        guard = (
            guard_factory(account_id, space_id)
            if callable(guard_factory) else nullcontext()
        )
        with guard:
            file = self.get_file(
                principal, account_id=account_id, space_id=space_id, file_id=file_id,
            )
            if file.generation != generation:
                raise ValueError("File changed; refresh and try again.")
            revisions = self.store.list_revisions(
                file.id, account_id=account_id, space_id=space_id,
            )
            refs = {
                file.id,
                f"drive_file:{file.id}",
                *(row.id for row in revisions),
                *(f"drive_revision:{row.id}" for row in revisions),
            }
            if target_is_held(
                self.platform_store.list_legal_holds(account_id),
                space_id=space_id,
                target_refs=refs,
            ):
                raise DriveConflictError("This Drive file is under an active legal hold.")
            deleting = self.store.update_file(replace(
                file,
                desired_indexed=False,
                active_doc_id="",
                index_status="deleting",
                generation=file.generation + 1,
            ), expected_generation=file.generation)

            # Prefix deletion also removes a promoted-but-uncommitted revision
            # left by an interrupted upload. Verification is fail-closed before
            # the identifying database rows or tombstone are committed.
            prefix = "/".join((
                drive_scope_prefix(file.tenant_id, account_id, space_id), file.id,
            ))
            blobs_deleted = self.blobs.delete_prefix(prefix)
            if self.blobs.delete_prefix(prefix):
                raise RuntimeError("Drive blob prefix deletion could not be verified.")
            counts = self.store.delete_file(
                file_id=deleting.id, account_id=account_id, space_id=space_id,
            )
            self.platform_store.create_tombstone(Tombstone(
                id=_id("tomb"),
                account_id=account_id,
                space_id=space_id,
                target_type="subject",
                target_ref=f"drive_file:{file.id}",
                reason=(reason or "drive_permanent_delete").strip(),
                created_by=principal.user_id,
                created_at=now_iso(),
            ))
            self._audit(
                principal, "drive.file.permanently_deleted", "drive_file", file.id, space_id,
                {**counts, "blobs_deleted": blobs_deleted},
            )
            return {**counts, "blobs_deleted": blobs_deleted}

    def _upload_for_principal(self, principal, upload_id: str) -> DriveUploadSession:
        upload = self.store.get_upload(upload_id, tenant_id=principal.tenant_id)
        if not upload or upload.created_by != principal.user_id:
            raise KeyError("Upload session not found.")
        self.authorize_space(principal, upload.account_id, upload.space_id)
        if upload.status not in {"completed", "completing", "expired"} and self._is_expired(upload.expires_at):
            self.blobs.delete_staging(upload.id)
            self.store.update_upload(replace(upload, status="expired", error="Upload session expired."))
            raise DriveConflictError("Upload session expired; start a new upload.")
        if upload.status == "expired":
            raise DriveConflictError("Upload session expired; start a new upload.")
        return upload

    def _enqueue_index(self, file: DriveFile) -> None:
        self.job_store.enqueue(
            type=JOB_DRIVE_FILE_INGEST,
            tenant_id=file.tenant_id,
            account_id=file.account_id,
            space_id=file.space_id,
            requested_by=file.uploaded_by,
            payload={
                "file_id": file.id,
                "revision_id": file.current_revision_id,
                "generation": file.generation,
            },
            max_attempts=self.settings.job_max_attempts,
            idempotency_key="drive:" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"onebrain:drive-job:{file.id}:{file.current_revision_id}:{file.generation}",
            ).hex,
        )

    def _reconcile_index_jobs(self, files) -> None:
        """Repair the metadata-to-queue crash window with generation deduplication."""

        if not self._indexing_allowed():
            return
        for file in files:
            if (
                file.index_status == "queued"
                and file.desired_indexed
                and not file.trashed_at
                and file.current_revision_id
            ):
                try:
                    self._enqueue_index(file)
                except Exception:
                    # Browsing remains available during a queue outage. The
                    # durable queued state causes the next read to retry.
                    continue

    def _folder_for_principal(
        self,
        principal,
        account_id: str,
        space_id: str,
        folder_id: str,
        *,
        space_kind: str,
        owner_user_id: str,
    ) -> DriveFolder:
        folder = self.store.get_folder(folder_id, account_id=account_id, space_id=space_id)
        if not folder:
            raise KeyError("Folder not found.")
        require_folder_access(
            principal, folder, space_kind=space_kind, owner_user_id=owner_user_id,
        )
        return folder

    def _effective_policy(
        self,
        principal,
        *,
        account_id: str,
        space_id: str,
        folder: DriveFolder | None,
        classification: str,
        location: str,
        category: str,
        index_for_ai: bool | None,
    ) -> tuple[str, str, str, bool]:
        base_classification = folder.default_classification if folder else "internal"
        requested_classification = (classification or base_classification).strip().lower()
        allowed_names = {item.name.lower() for item in Classification}
        if requested_classification not in allowed_names:
            raise ValueError("Unknown Drive classification.")
        base_level = Classification.parse(base_classification)
        requested_level = Classification.parse(requested_classification)
        if requested_level < base_level:
            raise PermissionError("A child item cannot widen its folder classification.")
        if int(requested_level) > int(principal.clearance):
            raise PermissionError("You cannot file an item above your clearance.")

        base_location = (folder.default_location if folder else GLOBAL_LOCATION).strip().lower()
        requested_location = (location or base_location).strip().lower()
        if not requested_location:
            raise ValueError("Drive location cannot be empty.")
        if base_location != GLOBAL_LOCATION and requested_location != base_location:
            raise PermissionError("A child item cannot replace its folder location audience.")
        if (
            requested_location != GLOBAL_LOCATION
            and principal.locations is not None
            and requested_location not in principal.locations
        ):
            raise PermissionError("You are not entitled to the selected Drive location.")

        base_category = (folder.default_category if folder else GENERAL_CATEGORY).strip()
        requested_category = (category or base_category).strip()
        if not requested_category:
            raise ValueError("Drive department cannot be empty.")
        if base_category != GENERAL_CATEGORY and requested_category != base_category:
            raise PermissionError("A child item cannot replace its folder department audience.")
        if requested_category != GENERAL_CATEGORY:
            group = next((
                row for row in self.platform_store.list_access_groups(account_id, space_id)
                if row.id == requested_category and row.status == "active"
                and row.space_id in {"", space_id}
            ), None)
            if not group:
                raise ValueError("Drive department is not active in this space.")
            if principal.categories is not None and requested_category not in principal.categories:
                raise PermissionError("You are not a member of the selected Drive department.")

        base_indexed = folder.default_indexed if folder else True
        requested_indexed = base_indexed if index_for_ai is None else bool(index_for_ai)
        if not base_indexed and requested_indexed:
            raise PermissionError("A child item cannot enable AI inside a non-indexed folder.")
        if not self._indexing_allowed():
            requested_indexed = False
        return (
            requested_level.name.lower(), requested_location, requested_category, requested_indexed,
        )

    def _drive_mode(self) -> str:
        mode = (getattr(self.settings, "drive_policy_mode", "storage_only") or "").strip().lower()
        if mode not in {"disabled", "storage_only", "storage_and_indexing"}:
            raise RuntimeError("ONEBRAIN_DRIVE_POLICY_MODE is invalid.")
        return mode

    def _indexing_allowed(self) -> bool:
        return self._drive_mode() == "storage_and_indexing"

    @staticmethod
    def _policy_widens(
        previous: tuple[str, str, str, bool], current: tuple[str, str, str, bool],
    ) -> bool:
        previous_classification, previous_location, previous_category, previous_indexed = previous
        current_classification, current_location, current_category, current_indexed = current
        return bool(
            Classification.parse(current_classification) < Classification.parse(previous_classification)
            or (
                previous_location != GLOBAL_LOCATION
                and current_location != previous_location
            )
            or (
                previous_category != GENERAL_CATEGORY
                and current_category != previous_category
            )
            or (current_indexed and not previous_indexed)
        )

    @staticmethod
    def _same_upload_request(left: DriveUploadSession, right: DriveUploadSession) -> bool:
        return (
            left.tenant_id, left.account_id, left.space_id, left.folder_id, left.name,
            left.size_bytes, left.desired_indexed, left.classification, left.location,
            left.category, left.created_by, left.idempotency_key,
        ) == (
            right.tenant_id, right.account_id, right.space_id, right.folder_id, right.name,
            right.size_bytes, right.desired_indexed, right.classification, right.location,
            right.category, right.created_by, right.idempotency_key,
        )

    @staticmethod
    def _is_expired(value: str) -> bool:
        if not value:
            return True
        normalized = value.replace("Z", "+00:00")
        try:
            expires = datetime.fromisoformat(normalized)
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)

    def _collect_tree_snapshot(
        self, root: DriveFolder, *, trashed: bool, operation_id: str = "",
    ) -> tuple[list[DriveFolder], list[DriveFile]]:
        folders = [root]
        files: list[DriveFile] = []
        queue = [root.id]
        while queue:
            parent_id = queue.pop(0)
            cursor = ""
            while True:
                page = self.store.list_entries(
                    account_id=root.account_id,
                    space_id=root.space_id,
                    folder_id=parent_id,
                    trashed=trashed,
                    cursor=cursor,
                    limit=250,
                )
                children = [
                    row for row in page.folders
                    if not operation_id or row.trash_operation_id == operation_id
                ]
                scoped_files = [
                    row for row in page.files
                    if not operation_id or row.trash_operation_id == operation_id
                ]
                folders.extend(children)
                files.extend(scoped_files)
                queue.extend(row.id for row in children)
                if len(folders) + len(files) > 10_000:
                    raise DriveLimitError("Folder tree exceeds the safe mutation limit.")
                cursor = page.next_cursor
                if not cursor:
                    break
        return folders, files

    def _require_tree_access(
        self,
        principal,
        folders: list[DriveFolder],
        files: list[DriveFile],
        *,
        space_kind: str,
        owner_user_id: str,
    ) -> None:
        for folder in folders:
            require_folder_access(
                principal, folder, space_kind=space_kind, owner_user_id=owner_user_id,
            )
        if any(not self._can_access(principal, file) for file in files):
            raise PermissionError(
                "Folder contains items outside your current audience; no changes were made."
            )

    def _audit(self, principal, action: str, target_type: str, target_id: str, space_id: str, meta=None):
        self.platform_store.record_audit(AuditEvent(
            id=_id("audit"), account_id=principal.tenant_id, space_id=space_id,
            actor_id=principal.user_id, actor_type=principal.principal_type,
            action=action, target_type=target_type, target_id=target_id,
            app_id="onebrain_core", purpose="knowledge_management", decision="allowed",
            meta=meta or {},
        ))

    @staticmethod
    def _deterministic_id(prefix: str, upload_id: str) -> str:
        return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, f'onebrain:drive:{prefix}:{upload_id}').hex}"

    def _can_access(self, principal, file: DriveFile) -> bool:
        try:
            self.authorize_space(principal, file.account_id, file.space_id)
            require_file_access(principal, file)
            return True
        except Exception:
            return False

    @staticmethod
    def _can_access_folder(
        principal, folder: DriveFolder, *, space_kind: str, owner_user_id: str,
    ) -> bool:
        try:
            require_folder_access(
                principal, folder, space_kind=space_kind, owner_user_id=owner_user_id,
            )
            return True
        except Exception:
            return False

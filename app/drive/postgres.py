"""PostgreSQL Drive metadata store and transactional AI projection publisher."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from uuid import NAMESPACE_URL, uuid5
from typing import Mapping, Optional, Sequence

import numpy as np

from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema
from app.drive.base import (
    DEFAULT_PAGE_SIZE,
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
    DriveQuarantineUsage,
    DriveQuarantinedCompletion,
    DriveRevision,
    DriveTreeMutationResult,
    DriveUploadSession,
    MalwareActivationState,
    ScannerRuntimeStatus,
    bounded_page_size,
    decode_page_cursor,
    encode_page_cursor,
    is_clean_attestation,
    malware_scan_matches_revision,
    normalize_file_list_detail_revision_ids,
    same_file_identity,
    same_folder_identity,
    same_revision_identity,
    validate_file,
    validate_folder,
    validate_malware_scan,
    validate_revision,
    validate_scanner_runtime_status,
    validate_upload,
)
from app.drive.malware.base import ScanVerdict
from app.store.base import Chunk


_FOLDER_COLUMNS = (
    "id, tenant_id, account_id, space_id, COALESCE(parent_id, ''), name, "
    "default_classification, default_location, default_category, default_indexed, "
    "generation, COALESCE(trashed_at::text, ''), COALESCE(original_parent_id, ''), "
    "trash_operation_id, created_by, created_at, updated_at"
)
_FILE_COLUMNS = (
    "id, tenant_id, account_id, space_id, COALESCE(folder_id, ''), name, classification, "
    "location, category, space_kind, owner_user_id, desired_indexed, approval_status, "
    "index_status, current_revision_id, active_doc_id, generation, uploaded_by, approved_by, "
    "COALESCE(trashed_at::text, ''), COALESCE(original_folder_id, ''), trash_operation_id, "
    "created_at, updated_at"
)
_REVISION_COLUMN_NAMES = (
    "id", "tenant_id", "account_id", "space_id", "file_id", "upload_session_id",
    "storage_key", "sha256", "size_bytes", "media_type", "original_name", "created_by",
    "created_at",
)
_REVISION_COLUMNS = ", ".join(_REVISION_COLUMN_NAMES)
_UPLOAD_COLUMNS = (
    "id, tenant_id, account_id, space_id, COALESCE(folder_id, ''), name, size_bytes, "
    "desired_indexed, classification, location, category, created_by, idempotency_key, "
    "staging_key, status, bytes_received, sha256, media_type, file_id, revision_id, error, "
    "quarantine_reserved_bytes, reservation_state, COALESCE(reservation_expires_at::text, ''), "
    "expires_at, created_at, updated_at"
)
_MALWARE_SCAN_APP_COLUMNS = (
    "id, tenant_id, account_id, space_id, file_id, revision_id, revision_sha256, "
    "revision_size_bytes, policy_epoch, status, origin, attempt_sequence, "
    "consecutive_failures, job_id, COALESCE(next_attempt_at::text, ''), ''::text, "
    "''::text, scanner_engine, scanner_engine_version, definition_version, "
    "COALESCE(definition_timestamp::text, ''), threat_code, error_code, "
    "COALESCE(started_at::text, ''), COALESCE(completed_at::text, ''), created_at, updated_at"
)
_SCANNER_RUNTIME_COLUMNS = (
    "tenant_id, worker_id, readiness, scanner_engine, scanner_engine_version, "
    "definition_version, COALESCE(definition_timestamp::text, ''), policy_epoch, "
    "COALESCE(last_successful_refresh_at::text, ''), "
    "COALESCE(last_successful_scan_at::text, ''), pending_count, recent_error_counts, "
    "heartbeat_at, created_at, updated_at"
)
_ACTIVATION_COLUMNS = (
    "singleton_id, schema_revision, policy_epoch, state, cursor, total_revisions, "
    "processed_revisions, legacy_bytes, quarantine_bytes, "
    "COALESCE(activated_at::text, ''), updated_at"
)


def _iso(value) -> str:
    if not value:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class PostgresDriveStore:
    def __init__(self, dsn: str, *, dim: int, worker_dsn: str = ""):
        import psycopg
        from pgvector.psycopg import register_vector

        self._psycopg = psycopg
        self._register_vector = register_vector
        self._dsn = dsn
        self._worker_dsn = worker_dsn
        self._dim = int(dim)
        with self._conn() as connection:
            validate_postgres_schema(connection, (
                "drive_folders", "drive_files", "drive_file_revisions", "drive_upload_sessions",
                "drive_revision_malware_scans", "drive_malware_runtime_status",
                "drive_malware_activation_state", "drive_malware_settings",
            ), require_malware_active=True)

    def _conn(self, *, tenant_id: str = "", account_id: str = "", space_id: str = ""):
        connection = self._psycopg.connect(self._dsn)
        self._register_vector(connection)
        if tenant_id or account_id or space_id:
            set_rls_scope(
                connection,
                tenant_id=tenant_id or account_id,
                account_id=account_id,
                space_id=space_id,
            )
        return connection

    def _worker_conn(self):
        if not self._worker_dsn:
            raise RuntimeError("Drive malware fenced operations require the worker database role.")
        return self._psycopg.connect(self._worker_dsn)

    @staticmethod
    def _folder_row(row) -> DriveFolder:
        return DriveFolder(
            id=row[0], tenant_id=row[1], account_id=row[2], space_id=row[3], parent_id=row[4],
            name=row[5], default_classification=row[6], default_location=row[7],
            default_category=row[8], default_indexed=row[9], generation=row[10],
            trashed_at=row[11], original_parent_id=row[12], trash_operation_id=row[13],
            created_by=row[14], created_at=_iso(row[15]), updated_at=_iso(row[16]),
        )

    @staticmethod
    def _file_row(row) -> DriveFile:
        return DriveFile(
            id=row[0], tenant_id=row[1], account_id=row[2], space_id=row[3], folder_id=row[4],
            name=row[5], classification=row[6], location=row[7], category=row[8],
            space_kind=row[9], owner_user_id=row[10], desired_indexed=row[11],
            approval_status=row[12], index_status=row[13], current_revision_id=row[14],
            active_doc_id=row[15], generation=row[16], uploaded_by=row[17], approved_by=row[18],
            trashed_at=row[19], original_folder_id=row[20], trash_operation_id=row[21],
            created_at=_iso(row[22]), updated_at=_iso(row[23]),
        )

    @staticmethod
    def _revision_row(row) -> DriveRevision:
        return DriveRevision(
            id=row[0], tenant_id=row[1], account_id=row[2], space_id=row[3], file_id=row[4],
            upload_session_id=row[5], storage_key=row[6], sha256=row[7], size_bytes=row[8],
            media_type=row[9], original_name=row[10], created_by=row[11], created_at=_iso(row[12]),
        )

    @staticmethod
    def _upload_row(row) -> DriveUploadSession:
        return DriveUploadSession(
            id=row[0], tenant_id=row[1], account_id=row[2], space_id=row[3], folder_id=row[4],
            name=row[5], size_bytes=row[6], desired_indexed=row[7], classification=row[8],
            location=row[9], category=row[10], created_by=row[11], idempotency_key=row[12],
            staging_key=row[13], status=row[14], bytes_received=row[15], sha256=row[16],
            media_type=row[17], file_id=row[18], revision_id=row[19], error=row[20],
            quarantine_reserved_bytes=row[21], reservation_state=row[22],
            reservation_expires_at=row[23], expires_at=_iso(row[24]),
            created_at=_iso(row[25]), updated_at=_iso(row[26]),
        )

    @staticmethod
    def _malware_scan_row(row) -> DriveMalwareScan:
        return DriveMalwareScan(
            id=row[0], tenant_id=row[1], account_id=row[2], space_id=row[3], file_id=row[4],
            revision_id=row[5], revision_sha256=row[6], revision_size_bytes=row[7],
            policy_epoch=row[8], status=row[9], origin=row[10], attempt_sequence=row[11],
            consecutive_failures=row[12], job_id=row[13], next_attempt_at=_iso(row[14]),
            attempt_fence=row[15], lease_expires_at=_iso(row[16]), scanner_engine=row[17],
            scanner_engine_version=row[18], definition_version=row[19],
            definition_timestamp=_iso(row[20]), threat_code=row[21], error_code=row[22],
            started_at=_iso(row[23]), completed_at=_iso(row[24]), created_at=_iso(row[25]),
            updated_at=_iso(row[26]),
        )

    @staticmethod
    def _scanner_runtime_row(row) -> ScannerRuntimeStatus:
        return ScannerRuntimeStatus(
            tenant_id=row[0], worker_id=row[1], readiness=row[2], scanner_engine=row[3],
            scanner_engine_version=row[4], definition_version=row[5],
            definition_timestamp=row[6], policy_epoch=row[7],
            last_successful_refresh_at=row[8], last_successful_scan_at=row[9],
            pending_count=row[10], recent_error_counts=dict(row[11] or {}),
            heartbeat_at=_iso(row[12]), created_at=_iso(row[13]), updated_at=_iso(row[14]),
        )

    @staticmethod
    def _activation_row(row) -> MalwareActivationState:
        return MalwareActivationState(
            singleton_id=row[0], schema_revision=row[1], policy_epoch=row[2], state=row[3],
            cursor=row[4], total_revisions=row[5], processed_revisions=row[6],
            legacy_bytes=row[7], quarantine_bytes=row[8], activated_at=row[9],
            updated_at=_iso(row[10]),
        )

    def create_folder(self, folder: DriveFolder) -> DriveFolder:
        validate_folder(folder)
        with self._conn(tenant_id=folder.tenant_id, account_id=folder.account_id, space_id=folder.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, folder.account_id, folder.space_id)
                self._validate_parent(cursor, folder.parent_id, folder.account_id, folder.space_id)
                try:
                    cursor.execute(
                        f"""
                        INSERT INTO drive_folders
                        (id, tenant_id, account_id, space_id, parent_id, name,
                         default_classification, default_location, default_category,
                         default_indexed, generation, created_by)
                        VALUES (%s, %s, %s, %s, NULLIF(%s, ''), %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING {_FOLDER_COLUMNS}
                        """,
                        (
                            folder.id, folder.tenant_id, folder.account_id, folder.space_id,
                            folder.parent_id, folder.name, folder.default_classification,
                            folder.default_location, folder.default_category, folder.default_indexed,
                            folder.generation, folder.created_by,
                        ),
                    )
                    row = cursor.fetchone()
                    if not row:
                        cursor.execute(
                            f"SELECT {_FOLDER_COLUMNS} FROM drive_folders "
                            "WHERE id=%s AND account_id=%s AND space_id=%s",
                            (folder.id, folder.account_id, folder.space_id),
                        )
                        existing_row = cursor.fetchone()
                        if (
                            not existing_row
                            or not same_folder_identity(self._folder_row(existing_row), folder)
                        ):
                            raise DriveConflictError(
                                "A folder with this name already exists here."
                            )
                        row = existing_row
                except self._psycopg.errors.UniqueViolation as exc:
                    raise DriveConflictError("A folder with this name already exists here.") from exc
            connection.commit()
        return self._folder_row(row)

    def get_folder(self, folder_id: str, *, account_id: str, space_id: str) -> Optional[DriveFolder]:
        if not folder_id:
            return None
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_FOLDER_COLUMNS} FROM drive_folders WHERE id=%s AND account_id=%s AND space_id=%s",
                (folder_id, account_id, space_id),
            )
            row = cursor.fetchone()
        return self._folder_row(row) if row else None

    def list_entries(
        self, *, account_id: str, space_id: str, folder_id: str = "", query: str = "",
        trashed: bool = False, cursor: str = "", limit: int = DEFAULT_PAGE_SIZE,
    ) -> DriveEntryPage:
        limit = bounded_page_size(limit)
        offset = decode_page_cursor(cursor)
        query = (query or "").strip()
        trash_clause = "trashed_at IS NOT NULL" if trashed else "trashed_at IS NULL"
        if query:
            folder_where = f"account_id=%s AND space_id=%s AND {trash_clause} AND name ILIKE %s"
            file_where = folder_where
            params: tuple = (account_id, space_id, f"%{query}%")
        else:
            folder_where = f"account_id=%s AND space_id=%s AND {trash_clause} AND COALESCE(parent_id, '')=%s"
            file_where = f"account_id=%s AND space_id=%s AND {trash_clause} AND COALESCE(folder_id, '')=%s"
            params = (account_id, space_id, folder_id)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor_obj:
            cursor_obj.execute(f"SELECT count(*) FROM drive_folders WHERE {folder_where}", params)
            folder_count = int(cursor_obj.fetchone()[0])
            folder_offset = min(offset, folder_count)
            cursor_obj.execute(
                f"SELECT {_FOLDER_COLUMNS} FROM drive_folders WHERE {folder_where} "
                "ORDER BY lower(name), id LIMIT %s OFFSET %s",
                (*params, limit + 1, folder_offset),
            )
            folder_rows = cursor_obj.fetchall()
            remaining = max(0, limit + 1 - len(folder_rows))
            file_offset = max(0, offset - folder_count)
            cursor_obj.execute(
                f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE {file_where} "
                "ORDER BY lower(name), id LIMIT %s OFFSET %s",
                (*params, remaining, file_offset),
            )
            file_rows = cursor_obj.fetchall()
        has_more = len(folder_rows) + len(file_rows) > limit
        folder_rows = folder_rows[:limit]
        file_rows = file_rows[: max(0, limit - len(folder_rows))]
        return DriveEntryPage(
            folders=tuple(self._folder_row(row) for row in folder_rows),
            files=tuple(self._file_row(row) for row in file_rows),
            next_cursor=encode_page_cursor(offset + limit) if has_more else "",
        )

    def breadcrumbs(self, folder_id: str, *, account_id: str, space_id: str) -> list[DriveFolder]:
        if not folder_id:
            return []
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH RECURSIVE ancestors AS (
                    SELECT *, 1 AS depth FROM drive_folders
                    WHERE id=%s AND account_id=%s AND space_id=%s
                    UNION ALL
                    SELECT parent.*, ancestors.depth + 1
                    FROM drive_folders parent
                    JOIN ancestors ON parent.id = ancestors.parent_id
                    WHERE ancestors.depth < %s
                )
                SELECT {_FOLDER_COLUMNS} FROM ancestors ORDER BY depth DESC
                """,
                (folder_id, account_id, space_id, MAX_FOLDER_DEPTH + 1),
            )
            rows = cursor.fetchall()
        if not rows or len(rows) > MAX_FOLDER_DEPTH:
            raise DriveLimitError("Folder hierarchy is missing, cyclic, or too deep.")
        return [self._folder_row(row) for row in rows]

    def update_folder(self, folder: DriveFolder, *, expected_generation: int) -> DriveFolder:
        validate_folder(folder)
        with self._conn(tenant_id=folder.tenant_id, account_id=folder.account_id, space_id=folder.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, folder.account_id, folder.space_id)
                self._validate_parent(cursor, folder.parent_id, folder.account_id, folder.space_id, moving_id=folder.id)
                try:
                    cursor.execute(
                        f"""
                        UPDATE drive_folders SET parent_id=NULLIF(%s,''), name=%s,
                            default_classification=%s, default_location=%s, default_category=%s,
                            default_indexed=%s, generation=%s, trashed_at=NULLIF(%s,'')::timestamptz,
                            original_parent_id=NULLIF(%s,''), trash_operation_id=%s, updated_at=now()
                        WHERE id=%s AND account_id=%s AND space_id=%s AND generation=%s
                        RETURNING {_FOLDER_COLUMNS}
                        """,
                        (
                            folder.parent_id, folder.name, folder.default_classification,
                            folder.default_location, folder.default_category, folder.default_indexed,
                            folder.generation, folder.trashed_at, folder.original_parent_id,
                            folder.trash_operation_id, folder.id, folder.account_id, folder.space_id,
                            expected_generation,
                        ),
                    )
                    row = cursor.fetchone()
                except self._psycopg.errors.UniqueViolation as exc:
                    raise DriveConflictError("A folder with this name already exists here.") from exc
                if not row:
                    raise DriveGenerationConflict("Folder changed; refresh and try again.")
            connection.commit()
        return self._folder_row(row)

    def trash_folder_tree(
        self, *, root: DriveFolder, expected_generation: int, operation_id: str,
        timestamp: str, folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
    ) -> DriveTreeMutationResult:
        with self._conn(tenant_id=root.tenant_id, account_id=root.account_id, space_id=root.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, root.account_id, root.space_id)
                folders, files = self._tree_snapshot(cursor, root, trashed=False)
                self._verify_tree_snapshot(
                    folders, files, folder_generations, file_generations,
                    root_id=root.id, expected_root_generation=expected_generation,
                )
                file_ids = [row.id for row in files]
                if file_ids:
                    cursor.execute(
                        "DELETE FROM chunks WHERE doc_id = ANY(%s) OR meta->>'drive_file_id' = ANY(%s)",
                        ([row.active_doc_id for row in files if row.active_doc_id], file_ids),
                    )
                    cursor.execute(
                        f"""
                        UPDATE drive_files SET trashed_at=%s::timestamptz,
                            original_folder_id=folder_id, trash_operation_id=%s,
                            active_doc_id='', index_status='not_indexed',
                            generation=generation+1, updated_at=now()
                        WHERE id = ANY(%s) RETURNING {_FILE_COLUMNS}
                        """,
                        (timestamp, operation_id, file_ids),
                    )
                    updated_files = [self._file_row(row) for row in cursor.fetchall()]
                else:
                    updated_files = []
                folder_ids = [row.id for row in folders]
                cursor.execute(
                    f"""
                    UPDATE drive_folders SET trashed_at=%s::timestamptz,
                        original_parent_id=parent_id, trash_operation_id=%s,
                        generation=generation+1, updated_at=now()
                    WHERE id = ANY(%s) RETURNING {_FOLDER_COLUMNS}
                    """,
                    (timestamp, operation_id, folder_ids),
                )
                updated_folders = [self._folder_row(row) for row in cursor.fetchall()]
            connection.commit()
        root_out = next(row for row in updated_folders if row.id == root.id)
        return DriveTreeMutationResult(root_out, tuple(updated_files))

    def restore_folder_tree(
        self, *, root: DriveFolder, expected_generation: int, operation_id: str,
        folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
        indexing_enabled: bool = True,
    ) -> DriveTreeMutationResult:
        with self._conn(tenant_id=root.tenant_id, account_id=root.account_id, space_id=root.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, root.account_id, root.space_id)
                folders, files = self._tree_snapshot(
                    cursor, root, trashed=True, operation_id=operation_id,
                )
                self._verify_tree_snapshot(
                    folders, files, folder_generations, file_generations,
                    root_id=root.id, expected_root_generation=expected_generation,
                )
                self._validate_parent(
                    cursor, root.parent_id, root.account_id, root.space_id, moving_id=root.id,
                )
                folder_ids = [row.id for row in folders]
                try:
                    cursor.execute(
                        f"""
                        UPDATE drive_folders SET
                            parent_id=CASE WHEN id=%s THEN NULLIF(%s,'') ELSE parent_id END,
                            trashed_at=NULL, original_parent_id=NULL, trash_operation_id='',
                            generation=generation+1, updated_at=now()
                        WHERE id = ANY(%s) RETURNING {_FOLDER_COLUMNS}
                        """,
                        (root.id, root.parent_id, folder_ids),
                    )
                    updated_folders = [self._folder_row(row) for row in cursor.fetchall()]
                except self._psycopg.errors.UniqueViolation as exc:
                    raise DriveConflictError(
                        "A folder with this name already exists at the restore destination."
                    ) from exc
                file_ids = [row.id for row in files]
                if file_ids:
                    cursor.execute(
                        f"""
                        UPDATE drive_files SET trashed_at=NULL, original_folder_id=NULL,
                            trash_operation_id='',
                            index_status=CASE
                                WHEN desired_indexed AND %s THEN 'queued' ELSE 'not_indexed'
                            END,
                            generation=generation+1, updated_at=now()
                        WHERE id = ANY(%s) RETURNING {_FILE_COLUMNS}
                        """,
                        (indexing_enabled, file_ids),
                    )
                    updated_files = [self._file_row(row) for row in cursor.fetchall()]
                else:
                    updated_files = []
            connection.commit()
        root_out = next(row for row in updated_folders if row.id == root.id)
        return DriveTreeMutationResult(root_out, tuple(updated_files))

    def create_file(self, file: DriveFile) -> DriveFile:
        validate_file(file)
        with self._conn(tenant_id=file.tenant_id, account_id=file.account_id, space_id=file.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, file.account_id, file.space_id)
                self._validate_parent(cursor, file.folder_id, file.account_id, file.space_id)
                cursor.execute(
                    f"""
                    INSERT INTO drive_files
                    (id, tenant_id, account_id, space_id, folder_id, name, classification,
                     location, category, space_kind, owner_user_id, desired_indexed,
                     approval_status, index_status, current_revision_id, active_doc_id,
                     generation, uploaded_by, approved_by)
                    VALUES (%s,%s,%s,%s,NULLIF(%s,''),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING {_FILE_COLUMNS}
                    """,
                    (
                        file.id, file.tenant_id, file.account_id, file.space_id, file.folder_id,
                        file.name, file.classification, file.location, file.category, file.space_kind,
                        file.owner_user_id, file.desired_indexed, file.approval_status,
                        file.index_status, file.current_revision_id, file.active_doc_id,
                        file.generation, file.uploaded_by, file.approved_by,
                    ),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        f"SELECT {_FILE_COLUMNS} FROM drive_files "
                        "WHERE id=%s AND account_id=%s AND space_id=%s",
                        (file.id, file.account_id, file.space_id),
                    )
                    existing_row = cursor.fetchone()
                    if not existing_row or not same_file_identity(self._file_row(existing_row), file):
                        raise DriveConflictError("File id already exists with different metadata.")
                    row = existing_row
            connection.commit()
        return self._file_row(row)

    def get_file(self, file_id: str, *, account_id: str, space_id: str) -> Optional[DriveFile]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s AND account_id=%s AND space_id=%s",
                (file_id, account_id, space_id),
            )
            row = cursor.fetchone()
        return self._file_row(row) if row else None

    def update_file(self, file: DriveFile, *, expected_generation: int) -> DriveFile:
        validate_file(file)
        with self._conn(tenant_id=file.tenant_id, account_id=file.account_id, space_id=file.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, file.account_id, file.space_id)
                cursor.execute(
                    "SELECT active_doc_id FROM drive_files WHERE id=%s AND account_id=%s AND space_id=%s "
                    "AND generation=%s FOR UPDATE",
                    (file.id, file.account_id, file.space_id, expected_generation),
                )
                current = cursor.fetchone()
                if not current:
                    raise DriveGenerationConflict("File changed; refresh and try again.")
                self._validate_parent(cursor, file.folder_id, file.account_id, file.space_id)
                if not file.active_doc_id:
                    cursor.execute(
                        "DELETE FROM chunks WHERE doc_id=%s OR meta->>'drive_file_id'=%s",
                        (current[0] or "", file.id),
                    )
                cursor.execute(
                    f"""
                    UPDATE drive_files SET folder_id=NULLIF(%s,''), name=%s, classification=%s,
                        location=%s, category=%s, space_kind=%s, owner_user_id=%s,
                        desired_indexed=%s, approval_status=%s, index_status=%s,
                        current_revision_id=%s, active_doc_id=%s, generation=%s,
                        approved_by=%s, trashed_at=NULLIF(%s,'')::timestamptz,
                        original_folder_id=NULLIF(%s,''), trash_operation_id=%s, updated_at=now()
                    WHERE id=%s AND account_id=%s AND space_id=%s AND generation=%s
                    RETURNING {_FILE_COLUMNS}
                    """,
                    (
                        file.folder_id, file.name, file.classification, file.location, file.category,
                        file.space_kind, file.owner_user_id, file.desired_indexed,
                        file.approval_status, file.index_status, file.current_revision_id,
                        file.active_doc_id, file.generation, file.approved_by, file.trashed_at,
                        file.original_folder_id, file.trash_operation_id, file.id, file.account_id,
                        file.space_id, expected_generation,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
        return self._file_row(row)

    def create_revision(self, revision: DriveRevision) -> DriveRevision:
        validate_revision(revision)
        with self._conn(tenant_id=revision.tenant_id, account_id=revision.account_id, space_id=revision.space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO drive_file_revisions
                    (id, tenant_id, account_id, space_id, file_id, upload_session_id,
                     storage_key, sha256, size_bytes, media_type, original_name, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    RETURNING {_REVISION_COLUMNS}
                    """,
                    (
                        revision.id, revision.tenant_id, revision.account_id, revision.space_id,
                        revision.file_id, revision.upload_session_id, revision.storage_key,
                        revision.sha256, revision.size_bytes, revision.media_type,
                        revision.original_name, revision.created_by,
                    ),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions "
                        "WHERE id=%s OR upload_session_id=%s",
                        (revision.id, revision.upload_session_id),
                    )
                    existing_row = cursor.fetchone()
                    if (
                        not existing_row
                        or not same_revision_identity(self._revision_row(existing_row), revision)
                    ):
                        raise DriveConflictError("Revision already exists with different metadata.")
                    row = existing_row
            connection.commit()
        return self._revision_row(row)

    def get_revision(
        self, revision_id: str, *, account_id: str, space_id: str,
    ) -> Optional[DriveRevision]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions WHERE id=%s AND account_id=%s AND space_id=%s",
                (revision_id, account_id, space_id),
            )
            row = cursor.fetchone()
        return self._revision_row(row) if row else None

    def get_file_list_details(
        self, *, account_id: str, space_id: str, revision_ids: Sequence[str],
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    ) -> Mapping[str, DriveFileListDetail]:
        requested = normalize_file_list_detail_revision_ids(revision_ids)
        if policy_epoch < 1:
            raise ValueError("Drive malware policy epoch must be positive.")
        if not requested:
            return {}
        revision_columns = ", ".join(
            f"revision.{column}" for column in _REVISION_COLUMN_NAMES
        )
        with self._conn(
            account_id=account_id,
            space_id=space_id,
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT current_revision.*, authoritative_scan.*
                FROM (
                    SELECT {revision_columns}
                    FROM drive_file_revisions AS revision
                    JOIN drive_files AS current_file
                      ON current_file.id=revision.file_id
                     AND current_file.tenant_id=revision.tenant_id
                     AND current_file.account_id=revision.account_id
                     AND current_file.space_id=revision.space_id
                     AND current_file.current_revision_id=revision.id
                    WHERE revision.account_id=%s
                      AND revision.space_id=%s
                      AND revision.id=ANY(%s::text[])
                ) AS current_revision
                LEFT JOIN LATERAL (
                    SELECT {_MALWARE_SCAN_APP_COLUMNS}
                    FROM drive_revision_malware_scans AS evidence
                    WHERE evidence.revision_id=current_revision.id
                      AND evidence.account_id=%s
                      AND evidence.space_id=%s
                      AND evidence.policy_epoch=%s
                    ORDER BY evidence.attempt_sequence DESC, evidence.id DESC
                    LIMIT 1
                ) AS authoritative_scan ON TRUE
                """,
                (account_id, space_id, list(requested), account_id, space_id, policy_epoch),
            )
            rows = cursor.fetchall()

        detail_offset = len(_REVISION_COLUMN_NAMES)
        details: dict[str, DriveFileListDetail] = {}
        for row in rows:
            revision = self._revision_row(row[:detail_offset])
            scan_row = row[detail_offset:]
            scan = self._malware_scan_row(scan_row) if scan_row and scan_row[0] else None
            if scan and not malware_scan_matches_revision(
                revision,
                scan,
                policy_epoch=policy_epoch,
            ):
                scan = None
            details[revision.id] = DriveFileListDetail(
                revision=revision,
                malware_scan=scan,
            )
        return details

    def list_revisions(self, file_id: str, *, account_id: str, space_id: str) -> list[DriveRevision]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions "
                "WHERE file_id=%s AND account_id=%s AND space_id=%s ORDER BY created_at DESC, id DESC",
                (file_id, account_id, space_id),
            )
            rows = cursor.fetchall()
        return [self._revision_row(row) for row in rows]

    def create_upload(self, upload: DriveUploadSession) -> DriveUploadSession:
        validate_upload(upload)
        with self._conn(tenant_id=upload.tenant_id, account_id=upload.account_id, space_id=upload.space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO drive_upload_sessions
                    (id, tenant_id, account_id, space_id, folder_id, name, size_bytes,
                     desired_indexed, classification, location, category, created_by,
                     idempotency_key, staging_key, status, expires_at)
                    VALUES (%s,%s,%s,%s,NULLIF(%s,''),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id, account_id, space_id, created_by, idempotency_key)
                    DO UPDATE SET updated_at=drive_upload_sessions.updated_at
                    RETURNING {_UPLOAD_COLUMNS}
                    """,
                    (
                        upload.id, upload.tenant_id, upload.account_id, upload.space_id,
                        upload.folder_id, upload.name, upload.size_bytes, upload.desired_indexed,
                        upload.classification, upload.location, upload.category, upload.created_by,
                        upload.idempotency_key, upload.staging_key, upload.status, upload.expires_at,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
        return self._upload_row(row)

    def reserve_upload(self, upload: DriveUploadSession) -> DriveUploadSession:
        candidate = replace(
            upload,
            quarantine_reserved_bytes=upload.size_bytes,
            reservation_state="reserved",
            reservation_expires_at=upload.reservation_expires_at or upload.expires_at,
        )
        validate_upload(candidate)
        with self._conn(
            tenant_id=candidate.tenant_id,
            account_id=candidate.account_id,
            space_id=candidate.space_id,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT public.onebrain_reserve_drive_quarantine"
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::timestamptz)",
                    (
                        candidate.id, candidate.tenant_id, candidate.account_id,
                        candidate.space_id, candidate.folder_id, candidate.name,
                        candidate.size_bytes, candidate.desired_indexed,
                        candidate.classification, candidate.location, candidate.category,
                        candidate.created_by, candidate.idempotency_key, candidate.staging_key,
                        candidate.expires_at,
                    ),
                )
                accepted = bool(cursor.fetchone()[0])
                if not accepted:
                    raise DriveQuarantineCapacityError()
                cursor.execute(
                    f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions "
                    "WHERE tenant_id=%s AND account_id=%s AND space_id=%s "
                    "AND created_by=%s AND idempotency_key=%s",
                    (
                        candidate.tenant_id, candidate.account_id, candidate.space_id,
                        candidate.created_by, candidate.idempotency_key,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
        if not row:
            raise RuntimeError("Quarantine reservation succeeded without an upload session.")
        return self._upload_row(row)

    def get_upload(self, upload_id: str, *, tenant_id: str = "") -> Optional[DriveUploadSession]:
        # Human tenants and platform account ids are currently the same hard
        # boundary. Drive RLS deliberately requires both settings, so a
        # tenant-only lookup would be invisible under FORCE RLS.
        with self._conn(tenant_id=tenant_id, account_id=tenant_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions WHERE id=%s AND tenant_id=%s",
                (upload_id, tenant_id),
            )
            row = cursor.fetchone()
        return self._upload_row(row) if row else None

    def get_upload_by_idempotency(
        self, *, account_id: str, space_id: str, created_by: str, idempotency_key: str,
    ) -> Optional[DriveUploadSession]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions "
                "WHERE account_id=%s AND space_id=%s AND created_by=%s AND idempotency_key=%s",
                (account_id, space_id, created_by, idempotency_key),
            )
            row = cursor.fetchone()
        return self._upload_row(row) if row else None

    def update_upload(self, upload: DriveUploadSession) -> DriveUploadSession:
        validate_upload(upload)
        with self._conn(tenant_id=upload.tenant_id, account_id=upload.account_id, space_id=upload.space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE drive_upload_sessions SET status=%s, bytes_received=%s, sha256=%s,
                        media_type=%s, file_id=%s, revision_id=%s, error=%s, updated_at=now()
                    WHERE id=%s AND tenant_id=%s AND account_id=%s AND space_id=%s
                    RETURNING {_UPLOAD_COLUMNS}
                    """,
                    (
                        upload.status, upload.bytes_received, upload.sha256, upload.media_type,
                        upload.file_id, upload.revision_id, upload.error, upload.id,
                        upload.tenant_id, upload.account_id, upload.space_id,
                    ),
                )
                row = cursor.fetchone()
                if not row:
                    raise KeyError("Upload session not found.")
            connection.commit()
        return self._upload_row(row)

    def release_upload_reservation(
        self, upload_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> DriveUploadSession:
        with self._conn(
            tenant_id=tenant_id, account_id=account_id, space_id=space_id
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT public.onebrain_release_drive_quarantine_reservation(%s,%s,%s,%s)",
                    (upload_id, tenant_id, account_id, space_id),
                )
                if not bool(cursor.fetchone()[0]):
                    raise KeyError("Upload session not found.")
                cursor.execute(
                    f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions "
                    "WHERE id=%s AND tenant_id=%s AND account_id=%s AND space_id=%s",
                    (upload_id, tenant_id, account_id, space_id),
                )
                row = cursor.fetchone()
            connection.commit()
        return self._upload_row(row)

    def list_expired_uploads(
        self, *, tenant_id: str, account_id: str, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]:
        bounded = max(1, min(int(limit), 5_000))
        with self._conn(tenant_id=tenant_id, account_id=account_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions "
                    "WHERE tenant_id=%s AND account_id=%s "
                    "AND status NOT IN ('completed','failed','expired') "
                    "AND expires_at <= %s::timestamptz "
                    "ORDER BY expires_at, id LIMIT %s",
                    (tenant_id, account_id, before, bounded),
                )
                rows = cursor.fetchall()
        return [self._upload_row(row) for row in rows]

    def list_expired_uploads_for_maintenance(
        self, *, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]:
        bounded = max(1, min(int(limit), 5_000))
        with self._worker_conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM public.onebrain_list_expired_drive_uploads"
                "(%s::timestamptz,%s)",
                (before, bounded),
            )
            rows = cursor.fetchall()
        return [self._upload_row(row) for row in rows]

    def complete_upload_quarantined(
        self, *, upload: DriveUploadSession, file: DriveFile, revision: DriveRevision,
        scan: DriveMalwareScan, scan_job_id: str, scan_job_max_attempts: int = 5,
    ) -> DriveQuarantinedCompletion:
        scan = replace(scan, job_id=scan_job_id)
        validate_upload(upload)
        validate_file(file)
        validate_revision(revision)
        validate_malware_scan(scan)
        if scan.status != "pending" or scan.origin != "upload":
            raise ValueError("Upload completion requires a pending upload malware attempt.")
        if not (
            revision.upload_session_id == upload.id
            and revision.file_id == file.id == scan.file_id
            and revision.id == scan.revision_id
            and revision.sha256 == scan.revision_sha256 == upload.sha256
            and revision.size_bytes == scan.revision_size_bytes == upload.size_bytes
        ):
            raise DriveConflictError("Quarantined upload records do not describe one revision.")
        scope = dict(
            tenant_id=upload.tenant_id, account_id=upload.account_id, space_id=upload.space_id
        )
        with self._conn(**scope) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(724918331)")
                cursor.execute(
                    f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions "
                    "WHERE id=%s AND tenant_id=%s AND account_id=%s AND space_id=%s FOR UPDATE",
                    (upload.id, upload.tenant_id, upload.account_id, upload.space_id),
                )
                raw_upload = cursor.fetchone()
                if not raw_upload:
                    raise KeyError("Upload session not found.")
                current_upload = self._upload_row(raw_upload)
                if current_upload.status == "completed":
                    cursor.execute(
                        f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s",
                        (current_upload.file_id,),
                    )
                    stored_file = self._file_row(cursor.fetchone())
                    cursor.execute(
                        f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions WHERE id=%s",
                        (current_upload.revision_id,),
                    )
                    stored_revision = self._revision_row(cursor.fetchone())
                    cursor.execute(
                        f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                        "WHERE revision_id=%s AND policy_epoch=%s "
                        "ORDER BY attempt_sequence DESC LIMIT 1",
                        (stored_revision.id, scan.policy_epoch),
                    )
                    stored_scan = self._malware_scan_row(cursor.fetchone())
                    return DriveQuarantinedCompletion(
                        current_upload, stored_file, stored_revision, stored_scan,
                        stored_scan.job_id,
                    )
                if (
                    current_upload.status not in {"uploaded", "completing"}
                    or current_upload.reservation_state != "reserved"
                ):
                    raise DriveConflictError("Upload is not ready for quarantine completion.")
                cursor.execute(
                    f"""
                    INSERT INTO drive_files (
                        id,tenant_id,account_id,space_id,folder_id,name,classification,
                        location,category,space_kind,owner_user_id,desired_indexed,
                        approval_status,index_status,current_revision_id,active_doc_id,
                        generation,uploaded_by
                    ) VALUES (%s,%s,%s,%s,NULLIF(%s,''),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'',%s,%s)
                    ON CONFLICT (id) DO NOTHING RETURNING {_FILE_COLUMNS}
                    """,
                    (
                        file.id,file.tenant_id,file.account_id,file.space_id,file.folder_id,
                        file.name,file.classification,file.location,file.category,file.space_kind,
                        file.owner_user_id,file.desired_indexed,file.approval_status,
                        "awaiting_scan" if file.desired_indexed else "not_indexed",
                        revision.id,file.generation,file.uploaded_by,
                    ),
                )
                raw_file = cursor.fetchone()
                if not raw_file:
                    cursor.execute(f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s", (file.id,))
                    raw_file = cursor.fetchone()
                stored_file = self._file_row(raw_file)
                if not same_file_identity(stored_file, file):
                    raise DriveConflictError("File id already exists with different metadata.")
                cursor.execute(
                    f"""
                    INSERT INTO drive_file_revisions (
                        id,tenant_id,account_id,space_id,file_id,upload_session_id,storage_key,
                        sha256,size_bytes,media_type,original_name,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING RETURNING {_REVISION_COLUMNS}
                    """,
                    (
                        revision.id,revision.tenant_id,revision.account_id,revision.space_id,
                        revision.file_id,revision.upload_session_id,revision.storage_key,
                        revision.sha256,revision.size_bytes,revision.media_type,
                        revision.original_name,revision.created_by,
                    ),
                )
                raw_revision = cursor.fetchone()
                if not raw_revision:
                    cursor.execute(
                        f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions WHERE id=%s",
                        (revision.id,),
                    )
                    raw_revision = cursor.fetchone()
                stored_revision = self._revision_row(raw_revision)
                if not same_revision_identity(stored_revision, revision):
                    raise DriveConflictError("Revision id already exists with different metadata.")
                cursor.execute(
                    f"""
                    INSERT INTO drive_revision_malware_scans (
                        id,tenant_id,account_id,space_id,file_id,revision_id,revision_sha256,
                        revision_size_bytes,policy_epoch,status,origin,attempt_sequence,
                        consecutive_failures,job_id,next_attempt_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,'')::timestamptz)
                    ON CONFLICT (revision_id,policy_epoch,attempt_sequence) DO NOTHING
                    RETURNING {_MALWARE_SCAN_APP_COLUMNS}
                    """,
                    (
                        scan.id,scan.tenant_id,scan.account_id,scan.space_id,scan.file_id,
                        scan.revision_id,scan.revision_sha256,scan.revision_size_bytes,
                        scan.policy_epoch,scan.status,scan.origin,scan.attempt_sequence,
                        scan.consecutive_failures,scan.job_id,scan.next_attempt_at,
                    ),
                )
                raw_scan = cursor.fetchone()
                if not raw_scan:
                    cursor.execute(
                        f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                        "WHERE revision_id=%s AND policy_epoch=%s AND attempt_sequence=%s",
                        (scan.revision_id, scan.policy_epoch, scan.attempt_sequence),
                    )
                    raw_scan = cursor.fetchone()
                stored_scan = self._malware_scan_row(raw_scan)
                if stored_scan.id != scan.id or stored_scan.job_id != scan.job_id:
                    raise DriveConflictError("Malware attempt identity already exists.")
                self._insert_or_verify_scan_job(
                    cursor,
                    job_id=scan_job_id,
                    tenant_id=scan.tenant_id,
                    account_id=scan.account_id,
                    space_id=scan.space_id,
                    requested_by=upload.created_by,
                    scan_id=scan.id,
                    revision_id=revision.id,
                    max_attempts=scan_job_max_attempts,
                )
                cursor.execute(
                    f"UPDATE drive_upload_sessions SET status='completed',file_id=%s,revision_id=%s,"
                    "reservation_state='transferred',reservation_expires_at=NULL,error='',updated_at=now() "
                    f"WHERE id=%s RETURNING {_UPLOAD_COLUMNS}",
                    (file.id, revision.id, upload.id),
                )
                stored_upload = self._upload_row(cursor.fetchone())
            connection.commit()
        return DriveQuarantinedCompletion(
            stored_upload, stored_file, stored_revision, stored_scan, scan_job_id
        )

    def create_malware_scan(self, scan: DriveMalwareScan) -> DriveMalwareScan:
        validate_malware_scan(scan)
        if scan.status not in {"pending", "rescan_required"}:
            raise PermissionError(
                "Application role may append only non-authorizing malware attempts."
            )
        with self._conn(
            tenant_id=scan.tenant_id, account_id=scan.account_id, space_id=scan.space_id
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO drive_revision_malware_scans (
                        id,tenant_id,account_id,space_id,file_id,revision_id,revision_sha256,
                        revision_size_bytes,policy_epoch,status,origin,attempt_sequence,
                        consecutive_failures,job_id,next_attempt_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        NULLIF(%s,'')::timestamptz
                    ) ON CONFLICT (revision_id,policy_epoch,attempt_sequence) DO NOTHING
                    RETURNING {_MALWARE_SCAN_APP_COLUMNS}
                    """,
                    (
                        scan.id,scan.tenant_id,scan.account_id,scan.space_id,scan.file_id,
                        scan.revision_id,scan.revision_sha256,scan.revision_size_bytes,
                        scan.policy_epoch,scan.status,scan.origin,scan.attempt_sequence,
                        scan.consecutive_failures,scan.job_id,scan.next_attempt_at,
                    ),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                        "WHERE revision_id=%s AND policy_epoch=%s AND attempt_sequence=%s",
                        (scan.revision_id, scan.policy_epoch, scan.attempt_sequence),
                    )
                    row = cursor.fetchone()
            connection.commit()
        stored = self._malware_scan_row(row)
        if stored.id != scan.id:
            raise DriveConflictError("Malware attempt sequence already exists.")
        return stored

    def get_malware_scan(
        self, scan_id: str, *, account_id: str, space_id: str,
    ) -> Optional[DriveMalwareScan]:
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                    "WHERE id=%s AND account_id=%s AND space_id=%s",
                    (scan_id, account_id, space_id),
                )
                row = cursor.fetchone()
        return self._malware_scan_row(row) if row else None

    def list_malware_scans(
        self, revision_id: str, *, account_id: str, space_id: str,
    ) -> list[DriveMalwareScan]:
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                    "WHERE revision_id=%s AND account_id=%s AND space_id=%s "
                    "ORDER BY policy_epoch,attempt_sequence,id",
                    (revision_id, account_id, space_id),
                )
                rows = cursor.fetchall()
        return [self._malware_scan_row(row) for row in rows]

    def get_authoritative_malware_scan(
        self, revision_id: str, *, account_id: str, space_id: str,
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    ) -> Optional[DriveMalwareScan]:
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                    "WHERE revision_id=%s AND account_id=%s AND space_id=%s AND policy_epoch=%s "
                    "ORDER BY attempt_sequence DESC,id DESC LIMIT 1",
                    (revision_id, account_id, space_id, policy_epoch),
                )
                row = cursor.fetchone()
        return self._malware_scan_row(row) if row else None

    def malware_scan_job_status(self, scan: DriveMalwareScan) -> str:
        with self._conn(
            tenant_id=scan.tenant_id,
            account_id=scan.account_id,
            space_id=scan.space_id,
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT type,status,tenant_id,account_id,space_id,payload,idempotency_key "
                "FROM jobs WHERE id=%s",
                (scan.job_id,),
            )
            row = cursor.fetchone()
        if not row:
            return ""
        payload = row[5]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        if not (
            row[0] == "drive_revision_malware_scan"
            and row[2] == scan.tenant_id
            and row[3] == scan.account_id
            and row[4] == scan.space_id
            and payload == {"scan_id": scan.id, "revision_id": scan.revision_id}
            and row[6] == f"drive-malware:{scan.id}"
        ):
            raise DriveConflictError("Malware scan job identity already exists.")
        return str(row[1])

    def update_malware_scan(
        self, scan: DriveMalwareScan, *, expected_status: str,
    ) -> DriveMalwareScan:
        raise PermissionError(
            "PostgreSQL malware evidence is mutated only through fenced database functions."
        )

    def upsert_scanner_runtime_status(
        self, status: ScannerRuntimeStatus,
    ) -> ScannerRuntimeStatus:
        validate_scanner_runtime_status(status)
        with self._worker_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO drive_malware_runtime_status (
                        tenant_id,worker_id,readiness,scanner_engine,scanner_engine_version,
                        definition_version,definition_timestamp,policy_epoch,
                        last_successful_refresh_at,last_successful_scan_at,pending_count,
                        recent_error_counts,heartbeat_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,NULLIF(%s,'')::timestamptz,%s,
                        NULLIF(%s,'')::timestamptz,NULLIF(%s,'')::timestamptz,%s,%s::jsonb,
                        COALESCE(NULLIF(%s,'')::timestamptz,now()))
                    ON CONFLICT (tenant_id,worker_id) DO UPDATE SET
                        readiness=EXCLUDED.readiness,scanner_engine=EXCLUDED.scanner_engine,
                        scanner_engine_version=EXCLUDED.scanner_engine_version,
                        definition_version=EXCLUDED.definition_version,
                        definition_timestamp=EXCLUDED.definition_timestamp,
                        policy_epoch=EXCLUDED.policy_epoch,
                        last_successful_refresh_at=EXCLUDED.last_successful_refresh_at,
                        last_successful_scan_at=EXCLUDED.last_successful_scan_at,
                        pending_count=EXCLUDED.pending_count,
                        recent_error_counts=EXCLUDED.recent_error_counts,
                        heartbeat_at=EXCLUDED.heartbeat_at,updated_at=now()
                    RETURNING {_SCANNER_RUNTIME_COLUMNS}
                    """,
                    (
                        status.tenant_id,status.worker_id,status.readiness,status.scanner_engine,
                        status.scanner_engine_version,status.definition_version,
                        status.definition_timestamp,status.policy_epoch,
                        status.last_successful_refresh_at,status.last_successful_scan_at,
                        status.pending_count,json.dumps(dict(status.recent_error_counts)),
                        status.heartbeat_at,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
        return self._scanner_runtime_row(row)

    def list_scanner_runtime_status(self, *, tenant_id: str) -> list[ScannerRuntimeStatus]:
        with self._conn(tenant_id=tenant_id, account_id=tenant_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SCANNER_RUNTIME_COLUMNS} FROM drive_malware_runtime_status "
                    "WHERE tenant_id=%s ORDER BY worker_id",
                    (tenant_id,),
                )
                rows = cursor.fetchall()
        return [self._scanner_runtime_row(row) for row in rows]

    def get_malware_activation_state(self) -> MalwareActivationState:
        with self._conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_ACTIVATION_COLUMNS} FROM drive_malware_activation_state "
                "WHERE singleton_id=true"
            )
            row = cursor.fetchone()
        if not row:
            raise RuntimeError("Drive malware activation state is missing.")
        return self._activation_row(row)

    def quarantine_limit_bytes(self) -> int:
        with self._conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT public.onebrain_drive_quarantine_limit()")
            row = cursor.fetchone()
        if not row or int(row[0]) <= 0:
            raise RuntimeError("Drive malware quarantine limit is unavailable.")
        return int(row[0])

    def quarantine_usage_bytes(self) -> int:
        return self.reconcile_quarantine_capacity().usage_bytes

    def reconcile_quarantine_capacity(self) -> DriveQuarantineUsage:
        # This is a deployment-wide operational aggregate. The tenant-facing
        # application role intentionally cannot execute the raw SECURITY
        # DEFINER function; only the separately wired worker capability can.
        with self._worker_conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM public.onebrain_drive_quarantine_usage()")
            row = cursor.fetchone()
        return DriveQuarantineUsage(
            usage_bytes=int(row[0]), reserved_bytes=int(row[1]),
            quarantined_bytes=int(row[2]),
        )

    def malware_operational_counts(
        self, *, tenant_id: str,
    ) -> DriveMalwareOperationalCounts:
        with self._conn(tenant_id=tenant_id, account_id=tenant_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (revision_id,policy_epoch)
                            revision_id,policy_epoch,status
                        FROM drive_revision_malware_scans
                        WHERE tenant_id=%s
                        ORDER BY revision_id,policy_epoch,attempt_sequence DESC,id DESC
                    ), pending AS (
                        SELECT count(*)::BIGINT AS count FROM latest
                        WHERE policy_epoch=%s
                          AND status IN ('pending','scanning','scan_error','rescan_required')
                    ), reserved AS (
                        SELECT COALESCE(sum(quarantine_reserved_bytes),0)::BIGINT AS bytes
                        FROM drive_upload_sessions
                        WHERE tenant_id=%s AND reservation_state='reserved'
                    ), quarantined AS (
                        SELECT COALESCE(sum(revision.size_bytes),0)::BIGINT AS bytes
                        FROM drive_file_revisions revision
                        LEFT JOIN latest ON latest.revision_id=revision.id
                          AND latest.policy_epoch=%s
                        WHERE revision.tenant_id=%s AND latest.status IS DISTINCT FROM 'clean'
                    )
                    SELECT pending.count,reserved.bytes,quarantined.bytes
                    FROM pending CROSS JOIN reserved CROSS JOIN quarantined
                    """,
                    (
                        tenant_id, DRIVE_MALWARE_POLICY_EPOCH, tenant_id,
                        DRIVE_MALWARE_POLICY_EPOCH, tenant_id,
                    ),
                )
                pending, reserved, quarantined = cursor.fetchone()
        return DriveMalwareOperationalCounts(
            pending_count=int(pending),
            quarantine_usage_bytes=int(reserved) + int(quarantined),
            quarantine_reserved_bytes=int(reserved),
            quarantined_revision_bytes=int(quarantined),
        )

    def list_malware_tenant_ids(
        self, *, after: str = "", limit: int = 1_000,
    ) -> list[str]:
        with self._worker_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT tenant_id FROM public.onebrain_list_drive_malware_tenants(%s,%s)",
                    ((after or "").strip(), max(1, min(int(limit), 1_000))),
                )
                rows = cursor.fetchall()
        return [str(row[0]) for row in rows]

    def list_pending_malware_job_specs(
        self, *, limit: int = 100,
    ) -> list[DriveMalwareJobSpec]:
        # PostgreSQL inserts the queue row in the same transaction; only the
        # JSON-backed memory store needs an external durable outbox drain.
        return []

    def acknowledge_malware_job_spec(self, job_id: str) -> None:
        return None

    def begin_malware_scan(
        self, *, job_id: str, lease_token: str, lease_expires_at: str,
        scan_id: str, attempt_fence: str,
    ) -> DriveMalwareScan:
        with self._worker_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT public.onebrain_begin_drive_malware_scan(%s,%s,%s,%s)",
                    (job_id, lease_token, scan_id, attempt_fence),
                )
                if not bool(cursor.fetchone()[0]):
                    raise DriveConflictError("Malware scan job lease or attempt is stale.")
                cursor.execute(
                    "SELECT * FROM public.onebrain_get_drive_malware_scan(%s)", (scan_id,)
                )
                row = cursor.fetchone()
            connection.commit()
        if not row:
            raise DriveConflictError("Malware scan attempt became unavailable.")
        return self._malware_scan_row(row)

    def complete_malware_scan(
        self, *, job_id: str, lease_token: str, scan_id: str, attempt_fence: str,
        verdict: ScanVerdict, next_attempt_at: str = "", consecutive_failures: int = 0,
    ) -> DriveMalwareCompletion:
        # The SECURITY DEFINER completion function derives the ingestion id
        # from the locked file/revision/generation tuple. Passing an empty
        # value prevents a stale caller snapshot from choosing that identity.
        ingestion_job_id = ""
        with self._worker_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM public.onebrain_complete_drive_malware_scan"
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,'')::timestamptz,%s,%s,"
                    "NULLIF(%s,'')::timestamptz,%s,%s)",
                    (
                        job_id,lease_token,scan_id,attempt_fence,verdict.status,
                        verdict.sha256,verdict.size_bytes,verdict.engine,verdict.engine_version,
                        verdict.definition_version,verdict.definition_timestamp,
                        verdict.threat_code,verdict.error_code,next_attempt_at,
                        consecutive_failures,ingestion_job_id,
                    ),
                )
                applied, created_ingestion = cursor.fetchone()
                cursor.execute(
                    "SELECT * FROM public.onebrain_get_drive_malware_scan(%s)", (scan_id,)
                )
                raw_scan = cursor.fetchone()
            connection.commit()
        if not applied:
            raise DriveConflictError("Malware scan completion lease or fence is stale.")
        scan = self._malware_scan_row(raw_scan)
        with self._conn(
            tenant_id=scan.tenant_id, account_id=scan.account_id, space_id=scan.space_id
        ) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s", (scan.file_id,))
            file_row = cursor.fetchone()
        return DriveMalwareCompletion(
            scan=scan,
            file=self._file_row(file_row) if file_row else None,
            ingestion_job_id=created_ingestion or "",
            applied=True,
        )

    def reconcile_malware_scans(
        self, *, limit: int = 100,
    ) -> DriveMalwareReconcileResult:
        with self._worker_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM public.onebrain_reconcile_drive_malware_scans(%s)",
                    (max(1, min(int(limit), 1_000)),),
                )
                row = cursor.fetchone()
            connection.commit()
        return DriveMalwareReconcileResult(int(row[0]), int(row[1]), int(row[2]))

    def wake_retryable_malware_scans(self, *, limit: int = 100) -> int:
        with self._worker_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT public.onebrain_wake_drive_malware_scan_errors(%s)",
                    (max(1, min(int(limit), 1_000)),),
                )
                row = cursor.fetchone()
            connection.commit()
        return int(row[0])

    def request_malware_rescan(
        self, *, file_id: str, account_id: str, space_id: str,
        expected_generation: int, requested_by: str, scan_id: str, scan_job_id: str,
        idempotency_key: str = "",
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH, scan_job_max_attempts: int = 5,
    ) -> tuple[DriveFile, DriveMalwareScan]:
        if policy_epoch != DRIVE_MALWARE_POLICY_EPOCH:
            raise DriveConflictError("Malware policy epoch is not active.")
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                replay = self._rescan_replay(
                    cursor,
                    file_id=file_id,
                    account_id=account_id,
                    space_id=space_id,
                    scan_id=scan_id,
                    scan_job_id=scan_job_id,
                    policy_epoch=policy_epoch,
                )
                if replay:
                    stored_file, stored_scan = replay
                    self._insert_or_verify_scan_job(
                        cursor,
                        job_id=scan_job_id,
                        tenant_id=stored_scan.tenant_id,
                        account_id=account_id,
                        space_id=space_id,
                        requested_by=requested_by,
                        scan_id=scan_id,
                        revision_id=stored_scan.revision_id,
                        max_attempts=scan_job_max_attempts,
                    )
                    connection.commit()
                    return stored_file, stored_scan
                cursor.execute(
                    f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s AND account_id=%s "
                    "AND space_id=%s AND generation=%s FOR UPDATE",
                    (file_id, account_id, space_id, expected_generation),
                )
                raw_file = cursor.fetchone()
                if not raw_file:
                    replay = self._rescan_replay(
                        cursor,
                        file_id=file_id,
                        account_id=account_id,
                        space_id=space_id,
                        scan_id=scan_id,
                        scan_job_id=scan_job_id,
                        policy_epoch=policy_epoch,
                    )
                    if replay:
                        stored_file, stored_scan = replay
                        self._insert_or_verify_scan_job(
                            cursor,
                            job_id=scan_job_id,
                            tenant_id=stored_scan.tenant_id,
                            account_id=account_id,
                            space_id=space_id,
                            requested_by=requested_by,
                            scan_id=scan_id,
                            revision_id=stored_scan.revision_id,
                            max_attempts=scan_job_max_attempts,
                        )
                        connection.commit()
                        return stored_file, stored_scan
                    raise DriveGenerationConflict("Drive file changed before malware rescan.")
                file = self._file_row(raw_file)
                cursor.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions WHERE id=%s "
                    "AND account_id=%s AND space_id=%s",
                    (file.current_revision_id, account_id, space_id),
                )
                raw_revision = cursor.fetchone()
                if not raw_revision:
                    raise DriveConflictError("Drive current revision is missing.")
                revision = self._revision_row(raw_revision)
                cursor.execute(
                    "SELECT COALESCE(max(attempt_sequence),0)+1 "
                    "FROM drive_revision_malware_scans WHERE revision_id=%s AND policy_epoch=%s",
                    (revision.id, policy_epoch),
                )
                sequence = int(cursor.fetchone()[0])
                # Chunk removal and new authoritative attempt share one commit.
                cursor.execute(
                    "DELETE FROM chunks WHERE doc_id=%s OR meta->>'drive_file_id'=%s",
                    (file.active_doc_id or "", file.id),
                )
                cursor.execute(
                    f"UPDATE drive_files SET active_doc_id='',index_status=%s,generation=generation+1,"
                    f"updated_at=now() WHERE id=%s RETURNING {_FILE_COLUMNS}",
                    ("awaiting_scan" if file.desired_indexed else "not_indexed", file.id),
                )
                stored_file = self._file_row(cursor.fetchone())
                cursor.execute(
                    f"""
                    INSERT INTO drive_revision_malware_scans (
                        id,tenant_id,account_id,space_id,file_id,revision_id,revision_sha256,
                        revision_size_bytes,policy_epoch,status,origin,attempt_sequence,job_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','rescan',%s,%s)
                    RETURNING {_MALWARE_SCAN_APP_COLUMNS}
                    """,
                    (
                        scan_id,revision.tenant_id,account_id,space_id,file.id,revision.id,
                        revision.sha256,revision.size_bytes,policy_epoch,sequence,scan_job_id,
                    ),
                )
                stored_scan = self._malware_scan_row(cursor.fetchone())
                self._insert_or_verify_scan_job(
                    cursor,
                    job_id=scan_job_id,
                    tenant_id=revision.tenant_id,
                    account_id=account_id,
                    space_id=space_id,
                    requested_by=requested_by,
                    scan_id=scan_id,
                    revision_id=revision.id,
                    max_attempts=scan_job_max_attempts,
                )
            connection.commit()
        return stored_file, stored_scan

    def _rescan_replay(
        self,
        cursor,
        *,
        file_id: str,
        account_id: str,
        space_id: str,
        scan_id: str,
        scan_job_id: str,
        policy_epoch: int,
    ) -> tuple[DriveFile, DriveMalwareScan] | None:
        cursor.execute(
            f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
            "WHERE id=%s AND account_id=%s AND space_id=%s",
            (scan_id, account_id, space_id),
        )
        raw_scan = cursor.fetchone()
        if not raw_scan:
            return None
        scan = self._malware_scan_row(raw_scan)
        if not (
            scan.file_id == file_id
            and scan.account_id == account_id
            and scan.space_id == space_id
            and scan.policy_epoch == policy_epoch
            and scan.origin == "rescan"
            and scan.job_id == scan_job_id
        ):
            raise DriveConflictError("Malware rescan idempotency identity conflicts.")
        cursor.execute(
            f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s AND account_id=%s "
            "AND space_id=%s",
            (file_id, account_id, space_id),
        )
        raw_file = cursor.fetchone()
        if not raw_file:
            raise DriveConflictError("Idempotent malware rescan file is unavailable.")
        file = self._file_row(raw_file)
        if file.current_revision_id != scan.revision_id:
            raise DriveConflictError("Idempotent malware rescan revision is no longer current.")
        return file, scan

    @staticmethod
    def _insert_or_verify_scan_job(
        cursor,
        *,
        job_id: str,
        tenant_id: str,
        account_id: str,
        space_id: str,
        requested_by: str,
        scan_id: str,
        revision_id: str,
        max_attempts: int,
    ) -> None:
        payload = {"scan_id": scan_id, "revision_id": revision_id}
        idempotency_key = f"drive-malware:{scan_id}"
        cursor.execute(
            "INSERT INTO jobs (id,type,status,tenant_id,account_id,space_id,requested_by,"
            "payload,max_attempts,idempotency_key) "
            "VALUES (%s,'drive_revision_malware_scan','queued',%s,%s,%s,%s,%s::jsonb,%s,%s) "
            "ON CONFLICT (tenant_id,account_id,space_id,type,idempotency_key) "
            "WHERE idempotency_key <> '' DO NOTHING",
            (
                job_id, tenant_id, account_id, space_id, requested_by,
                json.dumps(payload), max(1, int(max_attempts)), idempotency_key,
            ),
        )
        cursor.execute(
            "SELECT id,type,tenant_id,account_id,space_id,requested_by,payload,max_attempts,"
            "idempotency_key "
            "FROM jobs WHERE id=%s OR (tenant_id=%s AND account_id=%s AND space_id=%s "
            "AND type='drive_revision_malware_scan' AND idempotency_key=%s)",
            (job_id, tenant_id, account_id, space_id, idempotency_key),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise DriveConflictError("Malware scan job identity already exists.")
        row = rows[0]
        stored_payload = row[6]
        if isinstance(stored_payload, str):
            try:
                stored_payload = json.loads(stored_payload)
            except (TypeError, ValueError):
                stored_payload = None
        if not (
            row[0] == job_id
            and row[1] == "drive_revision_malware_scan"
            and row[2] == tenant_id
            and row[3] == account_id
            and row[4] == space_id
            and row[5] == requested_by
            and stored_payload == payload
            and int(row[7]) == max(1, int(max_attempts))
            and row[8] == idempotency_key
        ):
            raise DriveConflictError("Malware scan job identity already exists.")

    @staticmethod
    def _deterministic_job_id(key: str) -> str:
        return f"job_{uuid5(NAMESPACE_URL, f'onebrain:{key}').hex}"

    def publish_projection(
        self, *, file_id: str, revision_id: str, generation: int,
        account_id: str, space_id: str, chunks: Sequence[Chunk],
    ) -> DriveProjectionResult:
        if not chunks:
            raise ValueError("A Drive projection must contain chunks.")
        doc_id = chunks[0].doc_id
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE id=%s AND account_id=%s "
                    "AND space_id=%s FOR UPDATE",
                    (file_id, account_id, space_id),
                )
                raw = cursor.fetchone()
                if not raw:
                    raise DriveGenerationConflict("Drive indexing work is stale.")
                file = self._file_row(raw)
                if (
                    file.current_revision_id != revision_id or file.generation != generation
                    or file.trashed_at or not file.desired_indexed
                    or file.approval_status not in {"approved", "not_required"}
                ):
                    raise DriveGenerationConflict("Drive file is no longer eligible for indexing.")
                cursor.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions "
                    "WHERE id=%s AND file_id=%s AND account_id=%s AND space_id=%s",
                    (revision_id, file_id, account_id, space_id),
                )
                raw_revision = cursor.fetchone()
                cursor.execute(
                    f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans "
                    "WHERE revision_id=%s AND account_id=%s AND space_id=%s AND policy_epoch=%s "
                    "ORDER BY attempt_sequence DESC,id DESC LIMIT 1",
                    (revision_id, account_id, space_id, DRIVE_MALWARE_POLICY_EPOCH),
                )
                raw_scan = cursor.fetchone()
                revision = self._revision_row(raw_revision) if raw_revision else None
                scan = self._malware_scan_row(raw_scan) if raw_scan else None
                if not revision or not is_clean_attestation(revision, scan):
                    raise DriveGenerationConflict(
                        "Drive revision has no current clean attestation."
                    )
                cursor.execute(
                    "DELETE FROM chunks WHERE meta->>'drive_file_id'=%s",
                    (file.id,),
                )
                for chunk in chunks:
                    cursor.execute(
                        "INSERT INTO chunks (id, doc_id, text, meta, embedding, tenant_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (
                            chunk.id, chunk.doc_id, chunk.text, json.dumps(chunk.meta),
                            np.asarray(chunk.embedding), chunk.meta.get("tenant_id"),
                        ),
                    )
                cursor.execute(
                    f"UPDATE drive_files SET active_doc_id=%s, index_status='indexed', updated_at=now() "
                    f"WHERE id=%s RETURNING {_FILE_COLUMNS}",
                    (doc_id, file_id),
                )
                stored = self._file_row(cursor.fetchone())
            connection.commit()
        return DriveProjectionResult(stored, len(chunks))

    def unpublish(self, *, file_id: str, account_id: str, space_id: str, generation: int) -> DriveFile:
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT active_doc_id FROM drive_files WHERE id=%s AND account_id=%s AND space_id=%s "
                    "AND generation=%s FOR UPDATE",
                    (file_id, account_id, space_id, generation),
                )
                row = cursor.fetchone()
                if not row:
                    raise DriveGenerationConflict("Drive file changed.")
                cursor.execute(
                    "DELETE FROM chunks WHERE doc_id=%s OR meta->>'drive_file_id'=%s",
                    (row[0] or "", file_id),
                )
                cursor.execute(
                    f"UPDATE drive_files SET active_doc_id='', index_status='not_indexed', updated_at=now() "
                    f"WHERE id=%s RETURNING {_FILE_COLUMNS}",
                    (file_id,),
                )
                stored = self._file_row(cursor.fetchone())
            connection.commit()
        return stored

    def list_pending_review(self, *, account_id: str, space_id: str = "") -> list[DriveFile]:
        clause = "account_id=%s AND approval_status='pending' AND trashed_at IS NULL"
        params: list = [account_id]
        if space_id:
            clause += " AND space_id=%s"
            params.append(space_id)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE {clause} ORDER BY updated_at, id",
                tuple(params),
            )
            rows = cursor.fetchall()
        return [self._file_row(row) for row in rows]

    def list_clean_category_files(
        self, *, account_id: str, space_id: str, category: str,
        exclude_revision_ids: Sequence[str] = (), limit: int = 100,
    ) -> list[DriveFile]:
        """Non-trashed files in ``category`` whose current revision scanned clean.

        Tenant-scoped (the RLS GUCs bound this to one account+space, and the
        predicates re-assert it) and read-only — it reads malware evidence to
        confirm the authoritative attempt is ``clean`` but mutates nothing, so it
        needs no fenced worker function. Downstream per-category maintenance (the
        accounting extraction reconcile) uses it to re-derive stranded work from
        the durable clean verdict. ``exclude_revision_ids`` is applied *before*
        ``LIMIT`` so passing the already-processed revisions guarantees an older
        unprocessed file is still returned even past ``limit`` newer processed
        ones. The extraction handler re-validates the full clean attestation +
        blob before it reads bytes, so ``status='clean'`` on the authoritative
        attempt is a sufficient candidate filter here.
        """
        bounded = max(1, min(int(limit), 1_000))
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_FILE_COLUMNS} FROM drive_files "
                "WHERE account_id=%s AND space_id=%s AND category=%s "
                "AND current_revision_id <> ALL(%s::text[]) "
                "AND trashed_at IS NULL AND COALESCE(current_revision_id,'')<>'' "
                "AND EXISTS ("
                "  SELECT 1 FROM drive_revision_malware_scans s "
                "  WHERE s.revision_id=drive_files.current_revision_id "
                "    AND s.account_id=drive_files.account_id AND s.space_id=drive_files.space_id "
                "    AND s.policy_epoch=%s AND s.status='clean' "
                "    AND s.attempt_sequence=("
                "      SELECT MAX(s2.attempt_sequence) FROM drive_revision_malware_scans s2 "
                "      WHERE s2.revision_id=drive_files.current_revision_id "
                "        AND s2.account_id=drive_files.account_id "
                "        AND s2.space_id=drive_files.space_id AND s2.policy_epoch=%s"
                "    )"
                ") ORDER BY updated_at DESC, id DESC LIMIT %s",
                (
                    account_id, space_id, category, list(exclude_revision_ids),
                    DRIVE_MALWARE_POLICY_EPOCH, DRIVE_MALWARE_POLICY_EPOCH, bounded,
                ),
            )
            rows = cursor.fetchall()
        return [self._file_row(row) for row in rows]

    def delete_file(self, *, file_id: str, account_id: str, space_id: str) -> dict[str, int]:
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT active_doc_id FROM drive_files WHERE id=%s AND account_id=%s AND space_id=%s FOR UPDATE",
                    (file_id, account_id, space_id),
                )
                row = cursor.fetchone()
                if not row:
                    return {"files": 0, "revisions": 0, "chunks": 0}
                chunks = 0
                cursor.execute(
                    "DELETE FROM chunks WHERE doc_id=%s OR meta->>'drive_file_id'=%s",
                    (row[0] or "", file_id),
                )
                chunks = cursor.rowcount
                cursor.execute(
                    "SELECT count(*) FROM drive_revision_malware_scans WHERE file_id=%s", (file_id,)
                )
                malware_scans = int(cursor.fetchone()[0])
                cursor.execute("DELETE FROM drive_file_revisions WHERE file_id=%s", (file_id,))
                revisions = cursor.rowcount
                cursor.execute("DELETE FROM drive_files WHERE id=%s", (file_id,))
                files = cursor.rowcount
            connection.commit()
        return {
            "files": files, "revisions": revisions,
            "malware_scans": malware_scans, "chunks": chunks,
        }

    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict:
        clause = "tenant_id=%s AND account_id=%s"
        params: tuple = (tenant_id, account_id)
        if space_id:
            clause += " AND space_id=%s"
            params = (*params, space_id)
        with self._conn(tenant_id=tenant_id, account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {_FOLDER_COLUMNS} FROM drive_folders WHERE {clause}", params)
            folders = [asdict(self._folder_row(row)) for row in cursor.fetchall()]
            cursor.execute(f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE {clause}", params)
            files = [asdict(self._file_row(row)) for row in cursor.fetchall()]
            cursor.execute(f"SELECT {_REVISION_COLUMNS} FROM drive_file_revisions WHERE {clause}", params)
            revisions = [asdict(self._revision_row(row)) for row in cursor.fetchall()]
            cursor.execute(f"SELECT {_UPLOAD_COLUMNS} FROM drive_upload_sessions WHERE {clause}", params)
            uploads = [asdict(self._upload_row(row)) for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT {_MALWARE_SCAN_APP_COLUMNS} FROM drive_revision_malware_scans WHERE {clause}",
                params,
            )
            malware_scans = [asdict(self._malware_scan_row(row)) for row in cursor.fetchall()]
        return {
            "folders": folders, "files": files, "revisions": revisions,
            "uploads": uploads, "malware_scans": malware_scans,
        }

    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]:
        clause = "tenant_id=%s AND account_id=%s"
        params: tuple = (tenant_id, account_id)
        if space_id:
            clause += " AND space_id=%s"
            params = (*params, space_id)
        with self._conn(tenant_id=tenant_id, account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT active_doc_id FROM drive_files WHERE {clause} FOR UPDATE", params)
                doc_ids = [row[0] for row in cursor.fetchall() if row[0]]
                cursor.execute(f"SELECT id FROM drive_files WHERE {clause}", params)
                file_ids = [row[0] for row in cursor.fetchall()]
                if doc_ids or file_ids:
                    cursor.execute(
                        "DELETE FROM chunks WHERE doc_id = ANY(%s) OR meta->>'drive_file_id' = ANY(%s)",
                        (doc_ids, file_ids),
                    )
                counts = {}
                cursor.execute(
                    f"SELECT count(*) FROM drive_revision_malware_scans WHERE {clause}", params
                )
                counts["malware_scans"] = int(cursor.fetchone()[0])
                for key, table in (
                    ("uploads", "drive_upload_sessions"),
                    ("revisions", "drive_file_revisions"),
                    ("files", "drive_files"),
                    ("folders", "drive_folders"),
                ):
                    cursor.execute(f"DELETE FROM {table} WHERE {clause}", params)
                    counts[key] = cursor.rowcount
            connection.commit()
        return counts

    def _tree_snapshot(
        self, cursor, root: DriveFolder, *, trashed: bool, operation_id: str = "",
    ) -> tuple[list[DriveFolder], list[DriveFile]]:
        state_clause = "trashed_at IS NOT NULL" if trashed else "trashed_at IS NULL"
        operation_clause = "AND trash_operation_id=%s" if operation_id else ""
        parameters: list = [root.id, root.account_id, root.space_id]
        if operation_id:
            parameters.append(operation_id)
        parameters.extend((root.account_id, root.space_id))
        if operation_id:
            parameters.append(operation_id)
        cursor.execute(
            f"""
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM drive_folders
                WHERE id=%s AND account_id=%s AND space_id=%s
                    AND {state_clause} {operation_clause}
                UNION ALL
                SELECT child.id FROM drive_folders child
                JOIN tree parent ON child.parent_id=parent.id
                WHERE child.account_id=%s AND child.space_id=%s
                    AND child.{state_clause} {operation_clause}
            )
            SELECT id FROM tree LIMIT 10001
            """,
            tuple(parameters),
        )
        folder_ids = [row[0] for row in cursor.fetchall()]
        if not folder_ids or len(folder_ids) > 10_000:
            raise DriveLimitError("Folder tree is missing or exceeds the safe mutation limit.")
        cursor.execute(
            f"SELECT {_FOLDER_COLUMNS} FROM drive_folders WHERE id = ANY(%s) FOR UPDATE",
            (folder_ids,),
        )
        folders = [self._folder_row(row) for row in cursor.fetchall()]
        file_state = "trashed_at IS NOT NULL" if trashed else "trashed_at IS NULL"
        file_params: list = [folder_ids]
        file_operation = ""
        if operation_id:
            file_operation = "AND trash_operation_id=%s"
            file_params.append(operation_id)
        cursor.execute(
            f"SELECT {_FILE_COLUMNS} FROM drive_files WHERE folder_id = ANY(%s) "
            f"AND {file_state} {file_operation} FOR UPDATE",
            tuple(file_params),
        )
        files = [self._file_row(row) for row in cursor.fetchall()]
        if len(folders) + len(files) > 10_000:
            raise DriveLimitError("Folder tree exceeds the safe mutation limit.")
        return folders, files

    @staticmethod
    def _verify_tree_snapshot(
        folders: Sequence[DriveFolder], files: Sequence[DriveFile],
        folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
        *, root_id: str, expected_root_generation: int,
    ) -> None:
        actual_folders = {row.id: row.generation for row in folders}
        actual_files = {row.id: row.generation for row in files}
        if (
            actual_folders != dict(folder_generations)
            or actual_files != dict(file_generations)
            or actual_folders.get(root_id) != expected_root_generation
        ):
            raise DriveGenerationConflict("Folder contents changed; refresh and try again.")

    @staticmethod
    def _lock_scope(cursor, account_id: str, space_id: str) -> None:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"drive:{account_id}:{space_id}",))

    @staticmethod
    def _validate_parent(cursor, parent_id: str, account_id: str, space_id: str, *, moving_id: str = "") -> None:
        if not parent_id:
            return
        if parent_id == moving_id:
            raise DriveConflictError("A folder cannot contain itself.")
        cursor.execute(
            "SELECT 1 FROM drive_folders WHERE id=%s AND account_id=%s AND space_id=%s AND trashed_at IS NULL",
            (parent_id, account_id, space_id),
        )
        if not cursor.fetchone():
            raise KeyError("Parent folder not found.")
        cursor.execute(
            """
            WITH RECURSIVE ancestors(id, parent_id, depth) AS (
                SELECT id, parent_id, 1 FROM drive_folders
                WHERE id=%s AND account_id=%s AND space_id=%s AND trashed_at IS NULL
                UNION ALL
                SELECT parent.id, parent.parent_id, ancestors.depth + 1
                FROM drive_folders parent
                JOIN ancestors ON parent.id=ancestors.parent_id
                WHERE parent.account_id=%s AND parent.space_id=%s
                  AND parent.trashed_at IS NULL AND ancestors.depth <= %s
            )
            SELECT COALESCE(MAX(depth), 0) FROM ancestors
            """,
            (parent_id, account_id, space_id, account_id, space_id, MAX_FOLDER_DEPTH),
        )
        ancestor_depth = int(cursor.fetchone()[0])
        subtree_depth = 1
        if moving_id:
            cursor.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT id FROM drive_folders WHERE parent_id=%s
                    UNION ALL
                    SELECT child.id FROM drive_folders child JOIN descendants ON child.parent_id=descendants.id
                )
                SELECT 1 FROM descendants WHERE id=%s LIMIT 1
                """,
                (moving_id, parent_id),
            )
            if cursor.fetchone():
                raise DriveConflictError("A folder cannot move inside its descendant.")
            cursor.execute(
                """
                WITH RECURSIVE descendants(id, depth) AS (
                    SELECT id, 1 FROM drive_folders
                    WHERE id=%s AND account_id=%s AND space_id=%s
                    UNION ALL
                    SELECT child.id, descendants.depth + 1
                    FROM drive_folders child
                    JOIN descendants ON child.parent_id=descendants.id
                    WHERE child.account_id=%s AND child.space_id=%s
                      AND descendants.depth <= %s
                )
                SELECT COALESCE(MAX(depth), 1) FROM descendants
                """,
                (moving_id, account_id, space_id, account_id, space_id, MAX_FOLDER_DEPTH),
            )
            subtree_depth = int(cursor.fetchone()[0])
        if ancestor_depth + subtree_depth > MAX_FOLDER_DEPTH:
            raise DriveLimitError("Folder hierarchy is too deep.")

"""PostgreSQL Drive metadata store and transactional AI projection publisher."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Mapping, Optional, Sequence

import numpy as np

from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema
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
    same_file_identity,
    same_folder_identity,
    same_revision_identity,
    validate_file,
    validate_folder,
    validate_revision,
    validate_upload,
)
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
_REVISION_COLUMNS = (
    "id, tenant_id, account_id, space_id, file_id, upload_session_id, storage_key, sha256, "
    "size_bytes, media_type, original_name, created_by, created_at"
)
_UPLOAD_COLUMNS = (
    "id, tenant_id, account_id, space_id, COALESCE(folder_id, ''), name, size_bytes, "
    "desired_indexed, classification, location, category, created_by, idempotency_key, "
    "staging_key, status, bytes_received, sha256, media_type, file_id, revision_id, error, "
    "expires_at, created_at, updated_at"
)


def _iso(value) -> str:
    return value.isoformat() if value else ""


class PostgresDriveStore:
    def __init__(self, dsn: str, *, dim: int):
        import psycopg
        from pgvector.psycopg import register_vector

        self._psycopg = psycopg
        self._register_vector = register_vector
        self._dsn = dsn
        self._dim = int(dim)
        with self._conn() as connection:
            validate_postgres_schema(connection, (
                "drive_folders", "drive_files", "drive_file_revisions", "drive_upload_sessions",
            ))

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
            expires_at=_iso(row[21]), created_at=_iso(row[22]), updated_at=_iso(row[23]),
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
                cursor.execute("DELETE FROM drive_file_revisions WHERE file_id=%s", (file_id,))
                revisions = cursor.rowcount
                cursor.execute("DELETE FROM drive_files WHERE id=%s", (file_id,))
                files = cursor.rowcount
            connection.commit()
        return {"files": files, "revisions": revisions, "chunks": chunks}

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
        return {"folders": folders, "files": files, "revisions": revisions, "uploads": uploads}

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

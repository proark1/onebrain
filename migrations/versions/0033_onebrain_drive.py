"""Add the always-on OneBrain Drive metadata schema.

Revision ID: 0033_onebrain_drive
Revises: 0032_drive_foundations
Create Date: 2026-07-18
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "0033_onebrain_drive"
down_revision = "0032_drive_foundations"
branch_labels = None
depends_on = None

DRIVE_TABLES = (
    "drive_folders", "drive_files", "drive_file_revisions", "drive_upload_sessions",
)
APP_ROLE_ENV = "ONEBRAIN_POSTGRES_APP_ROLE"
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _app_role_ident() -> str:
    value = os.environ.get(APP_ROLE_ENV, "").strip()
    if not _ROLE_NAME.fullmatch(value):
        raise RuntimeError(f"{APP_ROLE_ENV} must name a simple PostgreSQL login role.")
    return f'"{value}"'


def _scope_policy(table: str) -> str:
    return f"""
        CREATE POLICY onebrain_{table}_scope ON {table}
        USING (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                AND account_id = current_setting('app.account_id', true)
                AND (current_setting('app.space_id', true) = ''
                     OR space_id = current_setting('app.space_id', true))
            )
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                AND account_id = current_setting('app.account_id', true)
                AND (current_setting('app.space_id', true) = ''
                     OR space_id = current_setting('app.space_id', true))
            )
        )
    """


def upgrade() -> None:
    app_ident = _app_role_ident()
    # Drive projection mutations and queue insertion use separate modular
    # stores. A durable generation key makes retries/reconciliation safe across
    # crashes without duplicating embedding work.
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS idempotency_key "
        "TEXT NOT NULL DEFAULT '' CHECK (char_length(idempotency_key) <= 200)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_idx "
        "ON jobs (tenant_id, account_id, space_id, type, idempotency_key) "
        "WHERE idempotency_key <> ''"
    )
    op.execute(
        f"GRANT SELECT (idempotency_key), INSERT (idempotency_key) ON TABLE jobs TO {app_ident}"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_folders (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES drive_folders(id) ON DELETE RESTRICT,
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 255),
            default_classification TEXT NOT NULL DEFAULT 'internal',
            default_location TEXT NOT NULL DEFAULT 'global',
            default_category TEXT NOT NULL DEFAULT 'general',
            default_indexed BOOLEAN NOT NULL DEFAULT true,
            generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
            trashed_at TIMESTAMPTZ,
            original_parent_id TEXT,
            trash_operation_id TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT drive_folders_scope_unique UNIQUE (id, tenant_id, account_id, space_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS drive_folders_active_sibling_name_idx "
        "ON drive_folders (tenant_id, account_id, space_id, COALESCE(parent_id, ''), lower(name)) "
        "WHERE trashed_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS drive_folders_children_idx "
        "ON drive_folders (tenant_id, account_id, space_id, parent_id, updated_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_files (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            folder_id TEXT REFERENCES drive_folders(id) ON DELETE SET NULL,
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 255),
            classification TEXT NOT NULL DEFAULT 'internal',
            location TEXT NOT NULL DEFAULT 'global',
            category TEXT NOT NULL DEFAULT 'general',
            space_kind TEXT NOT NULL DEFAULT '',
            owner_user_id TEXT NOT NULL DEFAULT '',
            desired_indexed BOOLEAN NOT NULL DEFAULT true,
            approval_status TEXT NOT NULL DEFAULT 'not_required'
                CHECK (approval_status IN ('not_required','pending','approved','rejected')),
            index_status TEXT NOT NULL DEFAULT 'not_indexed'
                CHECK (index_status IN ('not_indexed','queued','extracting','awaiting_review','indexing','indexed','blocked','unsupported','failed','stale','deleting')),
            current_revision_id TEXT NOT NULL DEFAULT '',
            active_doc_id TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
            uploaded_by TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            trashed_at TIMESTAMPTZ,
            original_folder_id TEXT,
            trash_operation_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT drive_files_scope_unique UNIQUE (id, tenant_id, account_id, space_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS drive_files_folder_idx "
        "ON drive_files (tenant_id, account_id, space_id, folder_id, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS drive_files_review_idx "
        "ON drive_files (tenant_id, account_id, space_id, approval_status, updated_at) "
        "WHERE approval_status = 'pending' AND trashed_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_file_revisions (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            file_id TEXT NOT NULL,
            upload_session_id TEXT NOT NULL UNIQUE,
            storage_key TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL CHECK (char_length(sha256) = 64),
            size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
            media_type TEXT NOT NULL,
            original_name TEXT NOT NULL CHECK (char_length(original_name) BETWEEN 1 AND 255),
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT drive_file_revisions_scope_unique UNIQUE (id, tenant_id, account_id, space_id),
            CONSTRAINT drive_file_revisions_file_scope_fk
                FOREIGN KEY (file_id, tenant_id, account_id, space_id)
                REFERENCES drive_files(id, tenant_id, account_id, space_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS drive_file_revisions_file_idx "
        "ON drive_file_revisions (tenant_id, account_id, space_id, file_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_upload_sessions (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            folder_id TEXT REFERENCES drive_folders(id) ON DELETE SET NULL,
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 255),
            size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
            desired_indexed BOOLEAN NOT NULL DEFAULT true,
            classification TEXT NOT NULL DEFAULT 'internal',
            location TEXT NOT NULL DEFAULT 'global',
            category TEXT NOT NULL DEFAULT 'general',
            created_by TEXT NOT NULL,
            idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
            staging_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created'
                CHECK (status IN ('created','uploading','uploaded','completing','completed','failed','expired')),
            bytes_received BIGINT NOT NULL DEFAULT 0 CHECK (bytes_received >= 0),
            sha256 TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            file_id TEXT NOT NULL DEFAULT '',
            revision_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT drive_upload_sessions_idempotency_unique
                UNIQUE (tenant_id, account_id, space_id, created_by, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS drive_upload_sessions_expiry_idx "
        "ON drive_upload_sessions (tenant_id, account_id, space_id, expires_at) "
        "WHERE status NOT IN ('completed','expired')"
    )

    for table in DRIVE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(_scope_policy(table))
        # Customer API calls and Drive indexing both use the restricted
        # application-data role. Queue claiming remains isolated on its worker DSN.
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {app_ident}")


def downgrade() -> None:
    raise RuntimeError(
        "0033_onebrain_drive is restore-required and cannot be downgraded destructively; "
        "roll back application images while retaining the additive schema."
    )

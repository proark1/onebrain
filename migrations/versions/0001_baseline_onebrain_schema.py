"""Baseline OneBrain Postgres schema.

Revision ID: 0001_baseline_onebrain_schema
Revises: None
Create Date: 2026-07-08
"""

from __future__ import annotations

import os

from alembic import op
from sqlalchemy import text


revision = "0001_baseline_onebrain_schema"
down_revision = None
branch_labels = None
depends_on = None

BASELINE_TABLES = (
    "chunks",
    "users",
    "conversations",
    "messages",
    "service_keys",
    "platform_accounts",
    "platform_spaces",
    "platform_app_installations",
    "platform_audit_events",
    "intake_records",
)


def _embedding_dim() -> int:
    raw = (
        os.environ.get("ONEBRAIN_MIGRATION_EMBEDDING_DIM")
        or os.environ.get("ONEBRAIN_EMBEDDING_DIM")
        or "256"
    )
    try:
        dim = int(raw)
    except ValueError as exc:
        raise ValueError("Embedding dimension must be an integer") from exc
    if dim <= 0:
        raise ValueError("Embedding dimension must be positive")
    return dim


def _assert_compatible_existing_chunks_table(dim: int) -> None:
    bind = op.get_bind()
    existing = bind.execute(
        text(
            "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c "
            "ON c.oid = a.attrelid WHERE c.relname = 'chunks' AND a.attname = 'embedding'"
        )
    ).fetchone()
    if existing is not None and existing[0] > 0 and existing[0] != dim:
        raise RuntimeError(
            "Existing pgvector chunks table has embedding dimension "
            f"{existing[0]}, but this migration is configured for {dim}. "
            "Run a re-embedding migration or point OneBrain at an empty/vector-compatible database."
        )


def upgrade() -> None:
    dim = _embedding_dim()

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    _assert_compatible_existing_chunks_table(dim)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            location TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL,
            text TEXT NOT NULL,
            meta JSONB NOT NULL,
            embedding vector({dim}),
            tenant_id TEXT
        )
        """
    )
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tenant_id TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS chunks_doc_id_idx ON chunks (doc_id)")
    op.execute("CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON chunks (tenant_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            account_id TEXT NOT NULL DEFAULT '',
            space_id TEXT NOT NULL DEFAULT ''
        )
        """
    )
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS space_id TEXT NOT NULL DEFAULT ''")
    op.execute(
        "CREATE INDEX IF NOT EXISTS conv_scope_idx "
        "ON conversations (tenant_id, session_id, role_id, account_id, space_id, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS conv_scope_space_idx "
        "ON conversations (tenant_id, session_id, role_id, account_id, space_id, updated_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            meta JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS msg_conv_idx ON messages (conversation_id, created_at)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS service_keys (
            id TEXT PRIMARY KEY,
            key_hash TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            scopes TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            app_id TEXT NOT NULL DEFAULT '',
            space_ids TEXT NOT NULL DEFAULT '',
            purposes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS app_id TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS space_ids TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS purposes TEXT NOT NULL DEFAULT ''")
    op.execute("CREATE INDEX IF NOT EXISTS service_keys_tenant_idx ON service_keys (tenant_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_accounts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            owner_user_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_spaces (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_spaces_account_idx ON platform_spaces (account_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_app_installations (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            app_id TEXT NOT NULL,
            enabled_space_ids TEXT NOT NULL,
            allowed_purposes TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_app_installations_account_idx "
        "ON platform_app_installations (account_id, app_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_audit_events (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            actor_type TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            space_id TEXT NOT NULL DEFAULT '',
            app_id TEXT NOT NULL DEFAULT '',
            purpose TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_audit_account_idx ON platform_audit_events (account_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS intake_records (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            space_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            source TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            record_type TEXT NOT NULL,
            intent TEXT NOT NULL,
            classification TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL,
            extracted_facts JSONB NOT NULL,
            metadata JSONB NOT NULL,
            created_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS intake_records_scope_idx "
        "ON intake_records (tenant_id, account_id, space_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS intake_records")
    op.execute("DROP TABLE IF EXISTS platform_audit_events")
    op.execute("DROP TABLE IF EXISTS platform_app_installations")
    op.execute("DROP TABLE IF EXISTS platform_spaces")
    op.execute("DROP TABLE IF EXISTS platform_accounts")
    op.execute("DROP TABLE IF EXISTS service_keys")
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversations")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS users")

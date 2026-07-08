"""Service-key lifecycle metadata.

Revision ID: 0003_service_key_lifecycle
Revises: 0002_postgres_worker_jobs
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op


revision = "0003_service_key_lifecycle"
down_revision = "0002_postgres_worker_jobs"
branch_labels = None
depends_on = None

SERVICE_KEY_LIFECYCLE_COLUMNS = (
    "last_used_at",
    "last_used_endpoint",
    "use_count",
    "rotated_from_id",
    "revoked_at",
)


def upgrade() -> None:
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS last_used_endpoint TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS rotated_from_id TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ")
    op.execute("CREATE INDEX IF NOT EXISTS service_keys_status_idx ON service_keys (tenant_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS service_keys_last_used_idx ON service_keys (tenant_id, last_used_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS service_keys_last_used_idx")
    op.execute("DROP INDEX IF EXISTS service_keys_status_idx")
    op.execute("ALTER TABLE service_keys DROP COLUMN IF EXISTS revoked_at")
    op.execute("ALTER TABLE service_keys DROP COLUMN IF EXISTS rotated_from_id")
    op.execute("ALTER TABLE service_keys DROP COLUMN IF EXISTS use_count")
    op.execute("ALTER TABLE service_keys DROP COLUMN IF EXISTS last_used_endpoint")
    op.execute("ALTER TABLE service_keys DROP COLUMN IF EXISTS last_used_at")

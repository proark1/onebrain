"""Add shared, hashed fixed-window login rate-limit counters.

Revision ID: 0029_auth_rate_limits
Revises: 0028_customer_teardown_protocol
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op


revision = "0029_auth_rate_limits"
down_revision = "0028_customer_teardown_protocol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve the historic deployment_type values/column, but ensure direct
    # database inserts no longer default new deployments to the retired provider.
    op.execute(
        "ALTER TABLE control_deployments "
        "ALTER COLUMN deployment_type SET DEFAULT 'dedicated_server'"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_rate_limits (
            scope TEXT NOT NULL CHECK (btrim(scope) <> ''),
            subject_hash TEXT NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
            window_started_at TIMESTAMPTZ NOT NULL,
            attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
            expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (scope, subject_hash, window_started_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS auth_rate_limits_expiry_idx "
        "ON auth_rate_limits (expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auth_rate_limits_expiry_idx")
    op.execute("DROP TABLE IF EXISTS auth_rate_limits")
    op.execute(
        "ALTER TABLE control_deployments "
        "ALTER COLUMN deployment_type SET DEFAULT 'dedicated_railway'"
    )

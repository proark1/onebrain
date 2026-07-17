"""Add ownership leases for recoverable direct AI employee turns.

Revision ID: 0026_ai_agent_run_leases
Revises: 0025_job_leases
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op


revision = "0026_ai_agent_run_leases"
down_revision = "0025_job_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_agent_runs
            ADD COLUMN IF NOT EXISTS lease_token TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ
        """
    )
    # Direct turns created before leases cannot safely resume after deployment:
    # their provider request may already have completed. Keep their idempotency
    # keys terminal rather than issuing a second paid request on retry.
    op.execute(
        """
        UPDATE ai_agent_runs
        SET status = 'failed',
            error = 'AI employee turn lease expired before completion.',
            completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
        WHERE status = 'running'
          AND mission_id = ''
          AND lease_token = ''
          AND lease_expires_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ai_agent_runs_lease_expiry_idx
        ON ai_agent_runs (tenant_id, account_id, space_id, lease_expires_at)
        WHERE status = 'running' AND lease_expires_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ai_agent_runs_lease_expiry_idx")
    op.execute(
        """
        ALTER TABLE ai_agent_runs
            DROP COLUMN IF EXISTS heartbeat_at,
            DROP COLUMN IF EXISTS lease_expires_at,
            DROP COLUMN IF EXISTS lease_token
        """
    )

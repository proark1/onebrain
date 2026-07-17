"""Add fenced, expiring worker-job leases.

Revision ID: 0026_job_leases
Revises: 0025_provisioning_module_selection
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op


revision = "0026_job_leases"
down_revision = "0025_provisioning_module_selection"
branch_labels = None
depends_on = None


JOB_LEASE_COLUMNS = ("lease_token", "lease_expires_at")


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_token TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ")
    # Pre-lease workers cannot renew. Preserve their original lock time so the
    # new claimant treats them as expired instead of leaving work stranded.
    op.execute(
        "UPDATE jobs SET lease_expires_at = locked_at "
        "WHERE status = 'running' AND lease_expires_at IS NULL AND locked_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS jobs_lease_claim_idx "
        "ON jobs (status, lease_expires_at, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_lease_claim_idx")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS lease_token")

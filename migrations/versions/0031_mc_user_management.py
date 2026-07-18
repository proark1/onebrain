"""Deployment-scoped Mission Control user-management jobs and receipts.

Revision ID: 0031_mc_user_management
Revises: 0030_job_queue_rls_roles
Create Date: 2026-07-18
"""

from alembic import op


revision = "0031_mc_user_management"
down_revision = "0030_job_queue_rls_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_user_management_jobs (
            id TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL REFERENCES control_deployments(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            requested_by TEXT NOT NULL,
            sealed_payload TEXT NOT NULL,
            sealed_result_private_key TEXT NOT NULL,
            result_public_key TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            leased_at TIMESTAMPTZ,
            lease_expires_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            completed_at TIMESTAMPTZ,
            result_sender_public_key TEXT NOT NULL DEFAULT '',
            result_nonce TEXT NOT NULL DEFAULT '',
            result_ciphertext TEXT NOT NULL DEFAULT '',
            result_expires_at TIMESTAMPTZ,
            result_consumed_at TIMESTAMPTZ,
            error_code TEXT NOT NULL DEFAULT '',
            CHECK (action IN ('directory.snapshot', 'user.create', 'user.password.reset',
                              'user.disable', 'user.enable', 'user.delete')),
            CHECK (status IN ('queued', 'leased', 'completed', 'failed', 'expired')),
            CHECK (attempts >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS fleet_user_management_jobs_lease_idx "
        "ON fleet_user_management_jobs (deployment_id, status, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS fleet_user_management_jobs_expiry_idx "
        "ON fleet_user_management_jobs (expires_at, result_expires_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_management_receipts (
            command_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            sender_public_key TEXT NOT NULL,
            nonce TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CHECK (action IN ('directory.snapshot', 'user.create', 'user.password.reset',
                              'user.disable', 'user.enable', 'user.delete'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS user_management_receipts_expiry_idx "
        "ON user_management_receipts (expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_management_receipts")
    op.execute("DROP TABLE IF EXISTS fleet_user_management_jobs")

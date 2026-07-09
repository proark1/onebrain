"""Provisioning runs and one-time secret envelopes.

Revision ID: 0006_provisioning_runs
Revises: 0005_control_plane_postgres
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op


revision = "0006_provisioning_runs"
down_revision = "0005_control_plane_postgres"
branch_labels = None
depends_on = None

PROVISIONING_TABLES = ("provisioning_runs", "one_time_secret_envelopes")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provisioning_runs (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            external_provider TEXT NOT NULL DEFAULT 'github_actions',
            external_run_id TEXT NOT NULL DEFAULT '',
            external_run_url TEXT NOT NULL DEFAULT '',
            request_payload JSONB NOT NULL DEFAULT '{}',
            result_payload JSONB NOT NULL DEFAULT '{}',
            railway_project_id TEXT NOT NULL DEFAULT '',
            railway_environment_id TEXT NOT NULL DEFAULT '',
            service_urls JSONB NOT NULL DEFAULT '{}',
            migration_revision TEXT NOT NULL DEFAULT '',
            smoke_status TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            bootstrap_secret_id TEXT NOT NULL DEFAULT '',
            retry_of_run_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            dispatched_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS provisioning_runs_account_idx ON provisioning_runs (account_id)")
    op.execute("CREATE INDEX IF NOT EXISTS provisioning_runs_deployment_idx ON provisioning_runs (deployment_id)")
    op.execute("CREATE INDEX IF NOT EXISTS provisioning_runs_status_idx ON provisioning_runs (status, created_at DESC)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS one_time_secret_envelopes (
            id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            account_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            nonce TEXT NOT NULL DEFAULT '',
            key_version TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS one_time_secret_envelopes_scope_idx "
        "ON one_time_secret_envelopes (account_id, deployment_id, expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS one_time_secret_envelopes_scope_idx")
    op.execute("DROP TABLE IF EXISTS one_time_secret_envelopes")
    op.execute("DROP INDEX IF EXISTS provisioning_runs_status_idx")
    op.execute("DROP INDEX IF EXISTS provisioning_runs_deployment_idx")
    op.execute("DROP INDEX IF EXISTS provisioning_runs_account_idx")
    op.execute("DROP TABLE IF EXISTS provisioning_runs")

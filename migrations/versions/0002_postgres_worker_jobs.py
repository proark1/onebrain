"""Postgres-backed worker jobs.

Revision ID: 0002_postgres_worker_jobs
Revises: 0001_baseline_onebrain_schema
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op


revision = "0002_postgres_worker_jobs"
down_revision = "0001_baseline_onebrain_schema"
branch_labels = None
depends_on = None

JOB_TABLES = ("jobs", "job_files")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL DEFAULT '',
            space_id TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{}',
            result JSONB,
            error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
            locked_by TEXT NOT NULL DEFAULT '',
            locked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX jobs_claim_idx ON jobs (status, run_after, created_at)")
    op.execute(
        "CREATE INDEX jobs_scope_idx "
        "ON jobs (tenant_id, account_id, space_id, created_at DESC)"
    )
    op.execute("CREATE INDEX jobs_locked_at_idx ON jobs (locked_at)")
    op.execute(
        """
        CREATE TABLE job_files (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL,
            data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX job_files_job_idx ON job_files (job_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_files")
    op.execute("DROP TABLE IF EXISTS jobs")

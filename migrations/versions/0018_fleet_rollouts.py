"""Fleet rollouts — the parent record of a ring-by-ring fleet-wide update.

A fleet rollout sweeps a release across the fleet ring by ring; its per-deployment
child rollouts (control_rollouts) carry fleet_rollout_id (added in 0017).

Revision ID: 0018_fleet_rollouts
Revises: 0017_rollout_execution
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op


revision = "0018_fleet_rollouts"
down_revision = "0017_rollout_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_fleet_rollouts (
            id                 TEXT PRIMARY KEY,
            target_version     TEXT NOT NULL,
            git_sha            TEXT NOT NULL DEFAULT '',
            status             TEXT NOT NULL DEFAULT 'pending',
            ring_order         JSONB NOT NULL DEFAULT '[]'::jsonb,
            current_ring       TEXT NOT NULL DEFAULT '',
            failure_tolerance  INTEGER NOT NULL DEFAULT 0,
            started_by         TEXT NOT NULL DEFAULT '',
            notes              TEXT NOT NULL DEFAULT '',
            callback_url       TEXT NOT NULL DEFAULT '',
            dry_run            BOOLEAN NOT NULL DEFAULT true,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_fleet_rollouts_status_idx "
        "ON control_fleet_rollouts (status, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS control_fleet_rollouts_status_idx")
    op.execute("DROP TABLE IF EXISTS control_fleet_rollouts")

"""Rollout execution — dispatch + callback state for real fleet updates.

`control_rollouts` gains execution columns so a rollout is not just a bookkeeping
row but a dispatched GitHub-Actions update job with a callback-driven lifecycle,
mirroring provisioning runs. All columns are additive with defaults, so the
existing start_rollout INSERT keeps working unchanged.

Revision ID: 0017_rollout_execution
Revises: 0016_deployment_account_id
Create Date: 2026-07-11

NOTE: this chains onto 0016 (the feat/deployment-account-id branch). The two must
land as one linear chain 0015 -> 0016 -> 0017; if 0016 is reordered, rebase this.
"""

from __future__ import annotations

from alembic import op


revision = "0017_rollout_execution"
down_revision = "0016_deployment_account_id"
branch_labels = None
depends_on = None


_COLUMNS = (
    "exec_status TEXT NOT NULL DEFAULT 'pending'",
    "external_provider TEXT NOT NULL DEFAULT 'github_actions'",
    "external_run_id TEXT NOT NULL DEFAULT ''",
    "external_run_url TEXT NOT NULL DEFAULT ''",
    "failure_reason TEXT NOT NULL DEFAULT ''",
    "request_payload JSONB NOT NULL DEFAULT '{}'::jsonb",
    "dispatched_at TIMESTAMPTZ NULL",
    "completed_at TIMESTAMPTZ NULL",
    "fleet_rollout_id TEXT NOT NULL DEFAULT ''",
)


def upgrade() -> None:
    for col in _COLUMNS:
        op.execute(f"ALTER TABLE control_rollouts ADD COLUMN IF NOT EXISTS {col}")
    op.execute("CREATE INDEX IF NOT EXISTS control_rollouts_exec_idx ON control_rollouts (deployment_id, exec_status)")
    op.execute("CREATE INDEX IF NOT EXISTS control_rollouts_fleet_idx ON control_rollouts (fleet_rollout_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS control_rollouts_fleet_idx")
    op.execute("DROP INDEX IF EXISTS control_rollouts_exec_idx")
    for col in ("exec_status", "external_provider", "external_run_id", "external_run_url",
                "failure_reason", "request_payload", "dispatched_at", "completed_at", "fleet_rollout_id"):
        op.execute(f"ALTER TABLE control_rollouts DROP COLUMN IF EXISTS {col}")

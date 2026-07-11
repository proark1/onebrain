"""Deployment -> account linkage for operator authorization.

`control_deployments` gains an authoritative `account_id` so the operator control
plane can scope per-deployment actions to the account that owns a deployment
instead of reverse-resolving from a display heuristic (customer_name). Additive
and backfilled to '' — existing rows keep working via the audit / dep_{account_id}
fallback until they are re-provisioned or updated.

Revision ID: 0016_deployment_account_id
Revises: 0015_fleet_telemetry
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op


revision = "0016_deployment_account_id"
down_revision = "0015_fleet_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE control_deployments ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT ''")
    op.execute("CREATE INDEX IF NOT EXISTS control_deployments_account_idx ON control_deployments (account_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS control_deployments_account_idx")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS account_id")

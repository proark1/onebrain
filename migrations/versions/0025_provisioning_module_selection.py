"""Replace preset provisioning bundles with explicit selected modules.

Revision ID: 0025_provisioning_module_selection
Revises: 0024_ai_employees_runtime
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op


revision = "0025_provisioning_module_selection"
down_revision = "0024_ai_employees_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Selected product modules are distinct from control_deployment_modules:
    # KPI Dashboard and AI Employees are real product choices even though they
    # currently add no separate container service row.
    op.execute(
        "ALTER TABLE control_deployments ADD COLUMN IF NOT EXISTS "
        "selected_module_ids JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE provisioning_runs ADD COLUMN IF NOT EXISTS "
        "module_ids JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE provisioning_runs DROP COLUMN IF EXISTS bundle_id")


def downgrade() -> None:
    # This is a development-only breaking contract.  A downgrade cannot infer a
    # historical preset bundle from an arbitrary combination, so retain a clear
    # legacy marker rather than fabricating a bundle selection.
    op.execute(
        "ALTER TABLE provisioning_runs ADD COLUMN IF NOT EXISTS "
        "bundle_id TEXT NOT NULL DEFAULT 'legacy_module_selection'"
    )
    op.execute("ALTER TABLE provisioning_runs DROP COLUMN IF EXISTS module_ids")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS selected_module_ids")

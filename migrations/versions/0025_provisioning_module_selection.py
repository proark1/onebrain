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
    # Alembic creates version_num as VARCHAR(32), but this published revision ID
    # is 34 characters. Widen the bookkeeping column before Alembic stamps the
    # new revision at the end of this transaction. Keep the wider type on
    # downgrade so a future descriptive revision cannot recreate the outage.
    op.execute(
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(128)"
    )
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

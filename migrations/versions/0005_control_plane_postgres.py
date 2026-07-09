"""Durable operator control-plane state.

Revision ID: 0005_control_plane_postgres
Revises: 0004_brand_theme_provisioning
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op


revision = "0005_control_plane_postgres"
down_revision = "0004_brand_theme_provisioning"
branch_labels = None
depends_on = None

CONTROL_PLANE_TABLES = (
    "control_deployments",
    "control_deployment_modules",
    "control_release_manifests",
    "control_backups",
    "control_health_checks",
    "control_rollouts",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_deployments (
            id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            environment TEXT NOT NULL DEFAULT 'production',
            deployment_type TEXT NOT NULL DEFAULT 'dedicated_railway',
            region TEXT NOT NULL DEFAULT '',
            release_ring TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'active',
            current_version TEXT NOT NULL DEFAULT '',
            current_migration TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS control_deployments_customer_idx ON control_deployments (customer_name)")
    op.execute("CREATE INDEX IF NOT EXISTS control_deployments_status_idx ON control_deployments (status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_deployment_modules (
            deployment_id TEXT NOT NULL REFERENCES control_deployments(id) ON DELETE CASCADE,
            module_id TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (deployment_id, module_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_deployment_modules_status_idx "
        "ON control_deployment_modules (deployment_id, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_release_manifests (
            version TEXT PRIMARY KEY,
            git_sha TEXT NOT NULL,
            modules JSONB NOT NULL,
            migration_from TEXT NOT NULL DEFAULT '',
            migration_to TEXT NOT NULL DEFAULT '',
            security_notes TEXT NOT NULL DEFAULT '',
            rollback_plan TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS control_release_status_idx ON control_release_manifests (status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_backups (
            id TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL REFERENCES control_deployments(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_backups_latest_idx "
        "ON control_backups (deployment_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_health_checks (
            id TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL REFERENCES control_deployments(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_health_latest_idx "
        "ON control_health_checks (deployment_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_rollouts (
            id TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL REFERENCES control_deployments(id) ON DELETE CASCADE,
            target_version TEXT NOT NULL REFERENCES control_release_manifests(version) ON DELETE RESTRICT,
            status TEXT NOT NULL,
            started_by TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_rollouts_deployment_idx "
        "ON control_rollouts (deployment_id, created_at DESC)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS control_rollouts_status_idx ON control_rollouts (status)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS control_rollouts_status_idx")
    op.execute("DROP INDEX IF EXISTS control_rollouts_deployment_idx")
    op.execute("DROP TABLE IF EXISTS control_rollouts")
    op.execute("DROP INDEX IF EXISTS control_health_latest_idx")
    op.execute("DROP TABLE IF EXISTS control_health_checks")
    op.execute("DROP INDEX IF EXISTS control_backups_latest_idx")
    op.execute("DROP TABLE IF EXISTS control_backups")
    op.execute("DROP INDEX IF EXISTS control_release_status_idx")
    op.execute("DROP TABLE IF EXISTS control_release_manifests")
    op.execute("DROP INDEX IF EXISTS control_deployment_modules_status_idx")
    op.execute("DROP TABLE IF EXISTS control_deployment_modules")
    op.execute("DROP INDEX IF EXISTS control_deployments_status_idx")
    op.execute("DROP INDEX IF EXISTS control_deployments_customer_idx")
    op.execute("DROP TABLE IF EXISTS control_deployments")

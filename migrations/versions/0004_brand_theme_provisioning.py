"""Brand themes for provisioned customers and tools.

Revision ID: 0004_brand_theme_provisioning
Revises: 0003_service_key_lifecycle
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op


revision = "0004_brand_theme_provisioning"
down_revision = "0003_service_key_lifecycle"
branch_labels = None
depends_on = None

BRAND_THEME_TABLES = ("platform_brand_themes",)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_brand_themes (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            app_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            primary_color TEXT NOT NULL,
            secondary_color TEXT NOT NULL,
            accent_color TEXT NOT NULL,
            background_color TEXT NOT NULL,
            surface_color TEXT NOT NULL,
            text_color TEXT NOT NULL,
            muted_color TEXT NOT NULL,
            success_color TEXT NOT NULL,
            warning_color TEXT NOT NULL,
            danger_color TEXT NOT NULL,
            logo_url TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'operator',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (account_id, app_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_brand_themes_account_idx "
        "ON platform_brand_themes (account_id, app_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform_brand_themes_account_idx")
    op.execute("DROP TABLE IF EXISTS platform_brand_themes")

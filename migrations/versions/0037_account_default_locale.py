"""Add the account default UI locale (i18n foundation).

The platform becomes bilingual (German primary, English available). The language
is chosen at customer provisioning, flows through the customer bootstrap descriptor
the same way ``module_ids`` do, and the box's reconcile persists it here — a durable,
queryable source the console seeds its language from, rather than a box.env value that
would be lost on the next render.

Additive and backfilled to 'de' (the platform default), so every account predating
the i18n foundation keeps working and reads as German until re-provisioned with an
explicit choice. Unlike the GoBD-retained accounting tables (0036), this is a pure
preference column and is safe to drop on downgrade.

Revision ID: 0037_account_default_locale
Revises: 0036_accounting_foundations
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op


revision = "0037_account_default_locale"
down_revision = "0036_accounting_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_accounts "
        "ADD COLUMN IF NOT EXISTS default_locale TEXT NOT NULL DEFAULT 'de'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE platform_accounts DROP COLUMN IF EXISTS default_locale")

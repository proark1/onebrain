"""First-login one-time password: force an owner minted at provision to rotate
their credential before doing anything privileged (P4-04, H-10).

Additive, idempotent, NOT NULL DEFAULT false so every existing INSERT in
app/users/postgres.py keeps working unchanged and every existing row behaves
exactly as before (false = the pre-0020 semantics).

Revision ID: 0020_must_change_password
Revises: 0019_trust_primitives
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op


revision = "0020_must_change_password"
down_revision = "0019_trust_primitives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS must_change_password")

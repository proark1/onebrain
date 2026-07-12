"""Hetzner P0 trust primitives — digest-pinned signed releases, per-deployment
update policy, and the restore_required dispatch acknowledgement.

All columns are additive with NOT NULL DEFAULTs so every existing INSERT in
app/controlplane/postgres.py keeps working unchanged and legacy rows behave
exactly as before (empty string / false = the pre-0019 semantics).

Revision ID: 0019_trust_primitives
Revises: 0018_fleet_rollouts
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op


revision = "0019_trust_primitives"
down_revision = "0018_fleet_rollouts"
branch_labels = None
depends_on = None


_RELEASE_COLUMNS = (
    "images JSONB NOT NULL DEFAULT '{}'::jsonb",
    "rollback_kind TEXT NOT NULL DEFAULT ''",
    "signature TEXT NOT NULL DEFAULT ''",
    "signing_key_id TEXT NOT NULL DEFAULT ''",
)


def upgrade() -> None:
    for col in _RELEASE_COLUMNS:
        op.execute(f"ALTER TABLE control_release_manifests ADD COLUMN IF NOT EXISTS {col}")
    op.execute("ALTER TABLE control_deployments ADD COLUMN IF NOT EXISTS update_policy TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE control_rollouts ADD COLUMN IF NOT EXISTS ack_restore_required BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE control_rollouts DROP COLUMN IF EXISTS ack_restore_required")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS update_policy")
    for col in ("images", "rollback_kind", "signature", "signing_key_id"):
        op.execute(f"ALTER TABLE control_release_manifests DROP COLUMN IF EXISTS {col}")

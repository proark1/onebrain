"""Phase 5 fleet secrets: served floor bumps, re-readable per-box secret
bundles, and single-use first-boot bootstrap tokens.

Additive/expand-only (rollback kind: code_only): three NEW tables, no column
added to any existing table, so every existing INSERT/SELECT in
app/controlplane/postgres.py, app/provisioning/runs.py, app/users/postgres.py is
untouched. Every statement is IF NOT EXISTS so a re-run is a no-op (mirrors 0006).

Revision ID: 0021_phase5_fleet_secrets
Revises: 0020_must_change_password
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op


revision = "0021_phase5_fleet_secrets"
down_revision = "0020_must_change_password"
branch_labels = None
depends_on = None

PHASE5_FLEET_SECRET_TABLES = (
    "control_served_floor_bumps",
    "box_secret_bundles",
    "box_bootstrap_tokens",
)


def upgrade() -> None:
    # P5-01 FloorBump serving (operator-set, pre-signed offline bump; survives MC restart).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_served_floor_bumps (
            scope         TEXT PRIMARY KEY,          -- '*' (fleet-wide) or a deployment_id
            bump_json     TEXT NOT NULL,             -- the signed FloorBump.model_dump_json() (opaque to MC)
            floor_version TEXT NOT NULL DEFAULT '',  -- denormalized for the operator list view
            updated_by    TEXT NOT NULL DEFAULT '',
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # P5-02 + P5-03: the re-readable per-box secret bundle + its rotation epoch.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS box_secret_bundles (
            deployment_id TEXT PRIMARY KEY,
            account_id    TEXT NOT NULL DEFAULT '',
            ciphertext    TEXT NOT NULL,             -- RE-READABLE raw-Fernet blob (seal_bundle/open_bundle,
                                                     -- NOT the one-time OneTimeSecretEnvelope).
            key_version   TEXT NOT NULL DEFAULT 'v1',
            secrets_epoch INTEGER NOT NULL DEFAULT 0,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # P5-03: the single-use, short-TTL first-boot bootstrap token (hash only).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS box_bootstrap_tokens (
            token_hash    TEXT PRIMARY KEY,          -- sha256$... (same hash shape as fleet/callback keys)
            deployment_id TEXT NOT NULL,
            account_id    TEXT NOT NULL DEFAULT '',
            expires_at    TIMESTAMPTZ NOT NULL,
            consumed_at   TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_box_bootstrap_tokens_deployment "
        "ON box_bootstrap_tokens (deployment_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_box_bootstrap_tokens_deployment")
    op.execute("DROP TABLE IF EXISTS box_bootstrap_tokens")
    op.execute("DROP TABLE IF EXISTS box_secret_bundles")
    op.execute("DROP TABLE IF EXISTS control_served_floor_bumps")

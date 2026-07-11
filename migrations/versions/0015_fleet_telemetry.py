"""Fleet telemetry — Mission Control heartbeat ingest, keys, and alerts.

These tables back the operator control plane (Mission Control): per-deployment
heartbeat keys, received metadata-only heartbeats, and the alert ledger. They
are operator-global (NOT tenant-scoped) and hold no customer content, so they
are deliberately outside the tenant RLS policies. They are created in every
OneBrain deployment (one image) but written only in an operator-mode deployment.

Revision ID: 0015_fleet_telemetry
Revises: 0014_tombstones
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op


revision = "0015_fleet_telemetry"
down_revision = "0014_tombstones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_keys (
            id            TEXT PRIMARY KEY,
            key_hash      TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            label         TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'active',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at  TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS fleet_keys_deployment_idx ON fleet_keys (deployment_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_heartbeats (
            id                 TEXT PRIMARY KEY,
            deployment_id      TEXT NOT NULL,
            contract_version   TEXT NOT NULL,
            reported_at        TIMESTAMPTZ NOT NULL,
            received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            healthy            BOOLEAN NOT NULL DEFAULT true,
            version            TEXT NOT NULL DEFAULT '',
            migration_revision TEXT NOT NULL DEFAULT '',
            payload            JSONB NOT NULL DEFAULT '{}'
        )
        """
    )
    # The hot read is "latest heartbeat per deployment".
    op.execute(
        "CREATE INDEX IF NOT EXISTS fleet_heartbeats_latest_idx "
        "ON fleet_heartbeats (deployment_id, received_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_alerts (
            id            TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL,
            kind          TEXT NOT NULL,
            detail        TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'open',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at   TIMESTAMPTZ
        )
        """
    )
    # Fast "is there an open alert of this kind?" and the open-alerts list.
    op.execute(
        "CREATE INDEX IF NOT EXISTS fleet_alerts_open_idx "
        "ON fleet_alerts (deployment_id, kind) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fleet_alerts")
    op.execute("DROP TABLE IF EXISTS fleet_heartbeats")
    op.execute("DROP TABLE IF EXISTS fleet_keys")

"""Persist governed KPI definitions and immutable snapshots.

Revision ID: 0023_kpi_dashboard_data
Revises: 0022_release_promotion_gate
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op


revision = "0023_kpi_dashboard_data"
down_revision = "0022_release_promotion_gate"
branch_labels = None
depends_on = None


KPI_TABLES = ("kpi_definitions", "kpi_snapshots")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_definitions (
            id                TEXT PRIMARY KEY,
            account_id        TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id          TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            key               TEXT NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{1,63}$'),
            name              TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
            description       TEXT NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
            category          TEXT NOT NULL DEFAULT '' CHECK (char_length(category) <= 80),
            unit              TEXT NOT NULL DEFAULT '' CHECK (char_length(unit) <= 32),
            source_label      TEXT NOT NULL DEFAULT '' CHECK (char_length(source_label) <= 120),
            owner_label       TEXT NOT NULL DEFAULT '' CHECK (char_length(owner_label) <= 120),
            freshness_minutes INTEGER NOT NULL DEFAULT 1440
                              CHECK (freshness_minutes BETWEEN 1 AND 525600),
            warning_min       NUMERIC(38,10),
            warning_max       NUMERIC(38,10),
            critical_min      NUMERIC(38,10),
            critical_max      NUMERIC(38,10),
            display_order     INTEGER NOT NULL DEFAULT 0
                              CHECK (display_order BETWEEN -1000000 AND 1000000),
            status            TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'archived')),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT kpi_definitions_threshold_min_check
                CHECK (critical_min IS NULL OR warning_min IS NULL OR critical_min <= warning_min),
            CONSTRAINT kpi_definitions_threshold_max_check
                CHECK (warning_max IS NULL OR critical_max IS NULL OR warning_max <= critical_max),
            CONSTRAINT kpi_definitions_scope_id_unique UNIQUE (id, account_id, space_id),
            CONSTRAINT kpi_definitions_space_key_unique UNIQUE (account_id, space_id, key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kpi_definitions_dashboard_idx "
        "ON kpi_definitions (account_id, space_id, status, display_order, name, id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kpi_snapshots (
            id                TEXT PRIMARY KEY,
            account_id        TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id          TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            kpi_id            TEXT NOT NULL,
            value             NUMERIC(38,10) NOT NULL,
            observed_at       TIMESTAMPTZ NOT NULL,
            received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            source_ref        TEXT NOT NULL DEFAULT '' CHECK (char_length(source_ref) <= 200),
            idempotency_key   TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
            created_by        TEXT NOT NULL CHECK (char_length(created_by) BETWEEN 1 AND 200),
            CONSTRAINT kpi_snapshots_definition_scope_fk
                FOREIGN KEY (kpi_id, account_id, space_id)
                REFERENCES kpi_definitions(id, account_id, space_id) ON DELETE CASCADE,
            CONSTRAINT kpi_snapshots_account_idempotency_unique
                UNIQUE (account_id, idempotency_key),
            CONSTRAINT kpi_snapshots_observation_unique
                UNIQUE (kpi_id, observed_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kpi_snapshots_history_idx "
        "ON kpi_snapshots (kpi_id, observed_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kpi_snapshots_retention_idx "
        "ON kpi_snapshots (account_id, space_id, received_at)"
    )

    for table in KPI_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(
            f"""
            CREATE POLICY onebrain_{table}_scope ON {table}
            USING (
                _onebrain_rls_admin()
                OR (
                    account_id = current_setting('app.account_id', true)
                    AND (
                        current_setting('app.space_id', true) = ''
                        OR space_id = current_setting('app.space_id', true)
                    )
                )
            )
            WITH CHECK (
                _onebrain_rls_admin()
                OR (
                    account_id = current_setting('app.account_id', true)
                    AND (
                        current_setting('app.space_id', true) = ''
                        OR space_id = current_setting('app.space_id', true)
                    )
                )
            )
            """
        )


def downgrade() -> None:
    for table in reversed(KPI_TABLES):
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP INDEX IF EXISTS kpi_snapshots_retention_idx")
    op.execute("DROP INDEX IF EXISTS kpi_snapshots_history_idx")
    op.execute("DROP TABLE IF EXISTS kpi_snapshots")
    op.execute("DROP INDEX IF EXISTS kpi_definitions_dashboard_idx")
    op.execute("DROP TABLE IF EXISTS kpi_definitions")

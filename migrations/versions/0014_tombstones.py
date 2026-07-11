"""Tombstones — a durable feed of erasures for modules to mirror.

When OneBrain erases a scope centrally (account offboarding, a subject-wide GDPR
request), the modules that hold their own operational copies must erase theirs
too. OneBrain records a content-free tombstone here; each module polls the feed
forward by `seq` and acks once it has applied the deletion. This is the reverse
of the module-initiated delete path — erasure decided at the canonical store,
propagated outward.

Account+space scoped under the same forced RLS as the other governance tables.

Revision ID: 0014_tombstones
Revises: 0013_legal_holds
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op


revision = "0014_tombstones"
down_revision = "0013_legal_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_tombstones (
            id          TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            seq         BIGSERIAL NOT NULL,
            space_id    TEXT NOT NULL DEFAULT '',
            target_type TEXT NOT NULL DEFAULT 'account',
            target_ref  TEXT NOT NULL DEFAULT '',
            reason      TEXT NOT NULL DEFAULT '',
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_tombstones_feed_idx "
        "ON platform_tombstones (account_id, seq)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_tombstone_acks (
            tombstone_id TEXT NOT NULL REFERENCES platform_tombstones(id) ON DELETE CASCADE,
            app_id       TEXT NOT NULL,
            account_id   TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            acked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tombstone_id, app_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_tombstone_acks_account_idx "
        "ON platform_tombstone_acks (account_id)"
    )

    for table in ("platform_tombstones", "platform_tombstone_acks"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")

    op.execute(
        """
        CREATE POLICY onebrain_platform_tombstones_scope ON platform_tombstones
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
    op.execute(
        """
        CREATE POLICY onebrain_platform_tombstone_acks_scope ON platform_tombstone_acks
        USING (
            _onebrain_rls_admin()
            OR account_id = current_setting('app.account_id', true)
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR account_id = current_setting('app.account_id', true)
        )
        """
    )


def downgrade() -> None:
    for table in ("platform_tombstone_acks", "platform_tombstones"):
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS platform_tombstone_acks")
    op.execute("DROP TABLE IF EXISTS platform_tombstones")

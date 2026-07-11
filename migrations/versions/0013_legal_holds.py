"""Legal holds — block retention and erasure for data under legal preservation.

A legal hold pins a scope (account, or a space, or a specific subject within it)
so that neither the retention sweep nor a GDPR erase can delete it. This closes
two gaps: `run_retention` and `erase_account_data` previously deleted regardless
of any preservation duty (there was no hold concept to consult). Precedence is
`legal hold > erasure > retention expiry` — a held scope is never deleted.

The table is account+space scoped and placed under the same forced RLS as the
other platform governance tables (policy mirrors 0008's account policy, reusing
the role-bound `_onebrain_rls_admin()` from 0009).

Revision ID: 0013_legal_holds
Revises: 0012_auth_sessions
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op


revision = "0013_legal_holds"
down_revision = "0012_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_legal_holds (
            id          TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id    TEXT NOT NULL DEFAULT '',
            subject_ref TEXT NOT NULL DEFAULT '',
            reason      TEXT NOT NULL DEFAULT '',
            legal_basis TEXT NOT NULL DEFAULT '',
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_legal_holds_account_idx "
        "ON platform_legal_holds (account_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_legal_holds_active_idx "
        "ON platform_legal_holds (account_id, space_id) WHERE released_at IS NULL"
    )

    op.execute("ALTER TABLE platform_legal_holds ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_legal_holds FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS onebrain_platform_legal_holds_scope ON platform_legal_holds")
    op.execute(
        """
        CREATE POLICY onebrain_platform_legal_holds_scope ON platform_legal_holds
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
    op.execute("DROP POLICY IF EXISTS onebrain_platform_legal_holds_scope ON platform_legal_holds")
    op.execute("ALTER TABLE platform_legal_holds NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_legal_holds DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS platform_legal_holds")

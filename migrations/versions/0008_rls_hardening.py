"""Enable forced row-level security for scoped customer data.

Revision ID: 0008_rls_hardening
Revises: 0007_governance_privacy
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op


revision = "0008_rls_hardening"
down_revision = "0007_governance_privacy"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "chunks",
    "conversations",
    "intake_records",
)

TENANT_ACCOUNT_SPACE_TABLES = (
    "conversations",
    "intake_records",
)

ACCOUNT_TABLES = (
    "platform_accounts",
    "platform_spaces",
    "platform_app_installations",
    "platform_brand_themes",
    "platform_audit_events",
    "platform_organizations",
    "platform_memberships",
    "platform_consent_records",
    "platform_retention_policies",
    "platform_data_access_events",
    "platform_credential_metadata",
    "retention_runs",
    "provisioning_runs",
    "one_time_secret_envelopes",
)

REGISTER_TABLES = (
    "platform_processor_register",
    "platform_provider_register",
)


def upgrade() -> None:
    for table in TENANT_TABLES:
        _enable_rls(table)
        _tenant_policy(table)

    _enable_rls("messages")
    op.execute("DROP POLICY IF EXISTS onebrain_messages_scope ON messages")
    op.execute(
        """
        CREATE POLICY onebrain_messages_scope ON messages
        USING (
            _onebrain_rls_admin()
            OR EXISTS (
                SELECT 1 FROM conversations c
                WHERE c.id = messages.conversation_id
                  AND c.tenant_id = current_setting('app.tenant_id', true)
                  AND (
                    current_setting('app.account_id', true) = ''
                    OR c.account_id = current_setting('app.account_id', true)
                  )
                  AND (
                    current_setting('app.space_id', true) = ''
                    OR c.space_id = current_setting('app.space_id', true)
                  )
            )
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR EXISTS (
                SELECT 1 FROM conversations c
                WHERE c.id = messages.conversation_id
                  AND c.tenant_id = current_setting('app.tenant_id', true)
                  AND (
                    current_setting('app.account_id', true) = ''
                    OR c.account_id = current_setting('app.account_id', true)
                  )
                  AND (
                    current_setting('app.space_id', true) = ''
                    OR c.space_id = current_setting('app.space_id', true)
                  )
            )
        )
        """
    )

    for table in ACCOUNT_TABLES:
        _enable_rls(table)
        _account_policy(table)

    for table in REGISTER_TABLES:
        _enable_rls(table)
        _register_policy(table)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS onebrain_messages_scope ON messages")
    _disable_rls("messages")
    for table in (*TENANT_TABLES, *ACCOUNT_TABLES, *REGISTER_TABLES):
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        _disable_rls(table)
    op.execute("DROP FUNCTION IF EXISTS _onebrain_rls_admin()")


def _enable_rls(table: str) -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION _onebrain_rls_admin()
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT current_setting('app.onebrain_admin', true) = 'true'
        $$
        """
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def _disable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def _tenant_policy(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
    account_space_clause = ""
    if table in TENANT_ACCOUNT_SPACE_TABLES:
        account_space_clause = """
                AND (
                    current_setting('app.account_id', true) = ''
                    OR account_id = current_setting('app.account_id', true)
                )
                AND (
                    current_setting('app.space_id', true) = ''
                    OR space_id = current_setting('app.space_id', true)
                )
        """
    op.execute(
        f"""
        CREATE POLICY onebrain_{table}_scope ON {table}
        USING (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                {account_space_clause}
            )
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                {account_space_clause}
            )
        )
        """
    )


def _account_policy(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
    if table == "platform_accounts":
        account_expr = "id = current_setting('app.account_id', true)"
    else:
        account_expr = "account_id = current_setting('app.account_id', true)"
    space_clause = ""
    if table in {
        "platform_audit_events",
        "platform_consent_records",
        "platform_retention_policies",
        "platform_data_access_events",
        "retention_runs",
    }:
        space_clause = """
                AND (
                    current_setting('app.space_id', true) = ''
                    OR space_id = current_setting('app.space_id', true)
                )
        """
    op.execute(
        f"""
        CREATE POLICY onebrain_{table}_scope ON {table}
        USING (
            _onebrain_rls_admin()
            OR (
                {account_expr}
                {space_clause}
            )
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR (
                {account_expr}
                {space_clause}
            )
        )
        """
    )


def _register_policy(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
    op.execute(
        f"""
        CREATE POLICY onebrain_{table}_scope ON {table}
        USING (
            _onebrain_rls_admin()
            OR account_id = ''
            OR account_id = current_setting('app.account_id', true)
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR account_id = ''
            OR account_id = current_setting('app.account_id', true)
        )
        """
    )

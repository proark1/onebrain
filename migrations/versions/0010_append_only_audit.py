"""Make the operational audit trail append-only.

`platform_audit_events` is written by `record_audit` (a plain INSERT) and read by
`list_audit` (SELECT). Nothing in the application updates or deletes it, and
privacy erase / governance deletion deliberately leave it intact (they delete the
data an audit event describes, then record a further audit event OF the erasure).

The gap analysis flagged that the application role nonetheless holds full CRUD on
every table, including audit. This migration enforces immutability at the database
level with triggers that reject UPDATE / DELETE / TRUNCATE, so the audit trail is
tamper-evident even against the application (and operator) role — only a deliberate
out-of-band action (disabling the trigger as table owner / superuser) can alter it.
INSERT is untouched, so `record_audit` keeps working.

Revision ID: 0010_append_only_audit
Revises: 0009_rls_admin_role
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op


revision = "0010_append_only_audit"
down_revision = "0009_rls_admin_role"
branch_labels = None
depends_on = None


_FORBID_FN = """
    CREATE OR REPLACE FUNCTION _onebrain_forbid_audit_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION 'platform_audit_events is append-only; % is not permitted', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$
"""


def upgrade() -> None:
    op.execute(_FORBID_FN)
    op.execute("DROP TRIGGER IF EXISTS onebrain_audit_append_only ON platform_audit_events")
    op.execute(
        """
        CREATE TRIGGER onebrain_audit_append_only
        BEFORE UPDATE OR DELETE ON platform_audit_events
        FOR EACH ROW EXECUTE FUNCTION _onebrain_forbid_audit_mutation()
        """
    )
    op.execute("DROP TRIGGER IF EXISTS onebrain_audit_no_truncate ON platform_audit_events")
    op.execute(
        """
        CREATE TRIGGER onebrain_audit_no_truncate
        BEFORE TRUNCATE ON platform_audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION _onebrain_forbid_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS onebrain_audit_no_truncate ON platform_audit_events")
    op.execute("DROP TRIGGER IF EXISTS onebrain_audit_append_only ON platform_audit_events")
    op.execute("DROP FUNCTION IF EXISTS _onebrain_forbid_audit_mutation()")

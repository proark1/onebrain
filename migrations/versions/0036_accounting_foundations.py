"""Add the empty accounting (Buchhaltung) module tables.

Phase 0 of the accounting module: two account/space-scoped, forced-RLS tables
that mirror the Drive schema pattern (0033). They ship EMPTY — no ingest or
extraction path writes to them yet; that lands in Phase 1. Creating them now
proves the module's storage layer end-to-end and lets the Postgres store validate
that the schema is migrated.

- ``accounting_documents`` — one row per invoice at the head/aggregate level
  (direction, parties, invoice identity/dates, per-document totals, dedup key,
  validation flags, pending/confirmed status, Drive references). Kontierung is
  intentionally NOT here — it belongs per posting on the line items.
- ``accounting_line_items`` — the postings, each carrying its own proposed and
  confirmed SKR03/04 account + tax key (+ optional cost centre), so mixed
  invoices split into separate bookings instead of one unusable summary row.

Revision ID: 0036_accounting_foundations
Revises: 0035_fleet_decommission_tombstone
Create Date: 2026-07-23
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "0036_accounting_foundations"
down_revision = "0035_fleet_decommission_tombstone"
branch_labels = None
depends_on = None

ACCOUNTING_TABLES = ("accounting_documents", "accounting_line_items")
APP_ROLE_ENV = "ONEBRAIN_POSTGRES_APP_ROLE"
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _app_role_ident() -> str:
    value = os.environ.get(APP_ROLE_ENV, "").strip()
    if not _ROLE_NAME.fullmatch(value):
        raise RuntimeError(f"{APP_ROLE_ENV} must name a simple PostgreSQL login role.")
    return f'"{value}"'


def _scope_policy(table: str) -> str:
    return f"""
        CREATE POLICY onebrain_{table}_scope ON {table}
        USING (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                AND account_id = current_setting('app.account_id', true)
                AND (current_setting('app.space_id', true) = ''
                     OR space_id = current_setting('app.space_id', true))
            )
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                AND account_id = current_setting('app.account_id', true)
                AND (current_setting('app.space_id', true) = ''
                     OR space_id = current_setting('app.space_id', true))
            )
        )
    """


def upgrade() -> None:
    app_ident = _app_role_ident()

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS accounting_documents (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            direction TEXT NOT NULL DEFAULT 'incoming'
                CHECK (direction IN ('incoming', 'outgoing')),
            issuer_name TEXT NOT NULL DEFAULT '',
            recipient_name TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT '',
            invoice_date DATE,
            service_date DATE,
            currency TEXT NOT NULL DEFAULT 'EUR',
            total_net NUMERIC(18, 2),
            total_tax NUMERIC(18, 2),
            total_gross NUMERIC(18, 2),
            tax_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
            dedup_key TEXT NOT NULL DEFAULT '',
            check_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed')),
            confidence NUMERIC(4, 3),
            jurisdiction TEXT NOT NULL DEFAULT 'DE',
            drive_file_id TEXT NOT NULL DEFAULT '',
            drive_revision_id TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            confirmed_by TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT accounting_documents_scope_unique UNIQUE (id, tenant_id, account_id, space_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS accounting_documents_workspace_idx "
        "ON accounting_documents (tenant_id, account_id, space_id, status, invoice_date DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS accounting_documents_dedup_idx "
        "ON accounting_documents (tenant_id, account_id, space_id, dedup_key) "
        "WHERE dedup_key <> ''"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS accounting_line_items (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            amount_net NUMERIC(18, 2),
            tax_rate NUMERIC(6, 3),
            amount_tax NUMERIC(18, 2),
            amount_gross NUMERIC(18, 2),
            proposed_account TEXT NOT NULL DEFAULT '',
            confirmed_account TEXT NOT NULL DEFAULT '',
            proposed_tax_key TEXT NOT NULL DEFAULT '',
            confirmed_tax_key TEXT NOT NULL DEFAULT '',
            cost_center TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT accounting_line_items_scope_unique UNIQUE (id, tenant_id, account_id, space_id),
            CONSTRAINT accounting_line_items_document_scope_fk
                FOREIGN KEY (document_id, tenant_id, account_id, space_id)
                REFERENCES accounting_documents(id, tenant_id, account_id, space_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS accounting_line_items_document_idx "
        "ON accounting_line_items (tenant_id, account_id, space_id, document_id, line_no)"
    )

    for table in ACCOUNTING_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(_scope_policy(table))
        # Customer API calls use the restricted application-data role, matching Drive.
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {app_ident}")


def downgrade() -> None:
    raise RuntimeError(
        "0036_accounting_foundations is restore-required and cannot be downgraded "
        "destructively; accounting documents are GoBD-retained. Roll back application "
        "images while retaining the additive schema."
    )

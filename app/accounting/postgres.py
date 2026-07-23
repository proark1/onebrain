"""Postgres-backed accounting store with account/space RLS scoping.

Phase 0 skeleton: overview + GDPR export/erase over the two accounting tables.
The extraction/ingest write-path lands in Phase 1; constructing this store
validates that the schema is migrated, and wiring the scope operations now keeps
GDPR export/erasure correct from the moment the first document can be created.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.accounting.base import AccountingOverview
from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema


_DOCUMENT_COLUMNS = (
    "id, tenant_id, account_id, space_id, direction, issuer_name, recipient_name, "
    "invoice_number, invoice_date, service_date, currency, total_net, total_tax, "
    "total_gross, tax_breakdown, dedup_key, check_flags, status, confidence, "
    "jurisdiction, drive_file_id, drive_revision_id, created_by, confirmed_by, "
    "created_at, updated_at"
)
_LINE_ITEM_COLUMNS = (
    "id, tenant_id, account_id, space_id, document_id, line_no, description, "
    "amount_net, tax_rate, amount_tax, amount_gross, proposed_account, "
    "confirmed_account, proposed_tax_key, confirmed_tax_key, cost_center, "
    "created_at, updated_at"
)


def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _row_to_dict(columns, row) -> dict:
    return {column: _json_safe(value) for column, value in zip(columns, row)}


class PostgresAccountingStore:
    def __init__(self, dsn: str, operator_dsn: str | None = None):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._operator_dsn = operator_dsn or dsn
        self._validate_schema()

    def _conn(self, *, account_id: str = "", space_id: str = "", admin: bool = False):
        connection = self._psycopg.connect(self._operator_dsn if admin else self._dsn)
        if account_id or space_id:
            # The 0036 policies require app.tenant_id (Drive's stronger shape), and
            # tenant_id == account_id on a customer box — mirror the Drive store or
            # every scoped read/write silently matches zero rows.
            set_rls_scope(
                connection, tenant_id=account_id, account_id=account_id, space_id=space_id,
            )
        return connection

    def _validate_schema(self) -> None:
        with self._conn() as connection:
            validate_postgres_schema(connection, ("accounting_documents", "accounting_line_items"))

    def overview(self, account_id: str, space_id: str) -> AccountingOverview:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*), "
                "count(*) FILTER (WHERE status = 'pending'), "
                "count(*) FILTER (WHERE status = 'confirmed') "
                "FROM accounting_documents WHERE account_id = %s AND space_id = %s",
                (account_id, space_id),
            )
            total, pending, confirmed = cursor.fetchone()
        return AccountingOverview(
            account_id=account_id,
            space_id=space_id,
            total_documents=total,
            pending_documents=pending,
            confirmed_documents=confirmed,
        )

    def export_scope(self, account_id: str, space_id: str = "") -> dict:
        clause = "account_id = %s"
        params: tuple = (account_id,)
        if space_id:
            clause += " AND space_id = %s"
            params = (account_id, space_id)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DOCUMENT_COLUMNS} FROM accounting_documents WHERE {clause} ORDER BY space_id, id",
                params,
            )
            document_columns = [description[0] for description in cursor.description]
            documents = [_row_to_dict(document_columns, row) for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT {_LINE_ITEM_COLUMNS} FROM accounting_line_items WHERE {clause} "
                "ORDER BY space_id, document_id, line_no, id",
                params,
            )
            line_item_columns = [description[0] for description in cursor.description]
            line_items = [_row_to_dict(line_item_columns, row) for row in cursor.fetchall()]
        return {"documents": documents, "line_items": line_items}

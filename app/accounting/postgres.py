"""Postgres-backed accounting store with account/space RLS scoping.

Phase 0 skeleton: overview + GDPR export/erase over the two accounting tables.
The extraction/ingest write-path lands in Phase 1; constructing this store
validates that the schema is migrated, and wiring the scope operations now keeps
GDPR export/erasure correct from the moment the first document can be created.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from app.accounting.base import AccountingOverview, build_summary
from app.accounting.model import INCOMING, OUTGOING
from app.accounting.validation import needs_review
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

_DOCUMENT_COLUMN_LIST = tuple(column.strip() for column in _DOCUMENT_COLUMNS.split(","))
_LINE_ITEM_COLUMN_LIST = tuple(column.strip() for column in _LINE_ITEM_COLUMNS.split(","))
_JSONB_COLUMNS = {"tax_breakdown", "check_flags"}

# psycopg3 sends str params with a text OID, and text→numeric/date/timestamptz has
# no implicit cast — so every typed column needs an explicit ``::type`` on its
# placeholder or the INSERT fails (only ever exercised in the Postgres CI job).
# Money/dates travel as strings-or-None (the JSON-safe row shape); None → NULL.
_COLUMN_CASTS = {
    "invoice_date": "::date",
    "service_date": "::date",
    "total_net": "::numeric",
    "total_tax": "::numeric",
    "total_gross": "::numeric",
    "confidence": "::numeric",
    "amount_net": "::numeric",
    "tax_rate": "::numeric",
    "amount_tax": "::numeric",
    "amount_gross": "::numeric",
    "tax_breakdown": "::jsonb",
    "check_flags": "::jsonb",
    "created_at": "::timestamptz",
    "updated_at": "::timestamptz",
}


def _placeholders(columns) -> str:
    return ", ".join(f"%s{_COLUMN_CASTS.get(column, '')}" for column in columns)


def _row_params(columns, row: dict) -> tuple:
    params = []
    for column in columns:
        value = row.get(column)
        if column in _JSONB_COLUMNS:
            default = {} if column == "check_flags" else []
            params.append(json.dumps(value if value is not None else default))
        else:
            params.append(value)
    return tuple(params)


_DOCUMENT_PLACEHOLDERS = _placeholders(_DOCUMENT_COLUMN_LIST)
_LINE_ITEM_PLACEHOLDERS = _placeholders(_LINE_ITEM_COLUMN_LIST)


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

    # ---- reads --------------------------------------------------------------

    def summary(self, account_id: str, space_id: str) -> dict:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*), "
                "count(*) FILTER (WHERE status = 'pending'), "
                "count(*) FILTER (WHERE status = 'confirmed') "
                "FROM accounting_documents WHERE account_id = %s AND space_id = %s",
                (account_id, space_id),
            )
            total, pending, confirmed = cursor.fetchone()
            cursor.execute(
                "SELECT direction, count(*), coalesce(sum(total_net), 0), "
                "coalesce(sum(total_tax), 0), coalesce(sum(total_gross), 0) "
                "FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s AND status = 'confirmed' "
                # EUR-only money (non-EUR invoices are flagged, never summed here).
                "AND upper(currency) = 'EUR' "
                "GROUP BY direction",
                (account_id, space_id),
            )
            sides = {
                INCOMING: {"count": 0, "net": Decimal("0"), "tax": Decimal("0"), "gross": Decimal("0")},
                OUTGOING: {"count": 0, "net": Decimal("0"), "tax": Decimal("0"), "gross": Decimal("0")},
            }
            for direction, count, net, tax, gross in cursor.fetchall():
                if direction in sides:
                    sides[direction] = {"count": count, "net": net, "tax": tax, "gross": gross}
        return build_summary(
            account_id, space_id,
            total=total, pending=pending, confirmed=confirmed,
            incoming=sides[INCOMING], outgoing=sides[OUTGOING],
        )

    def get_document(self, account_id: str, space_id: str, document_id: str):
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DOCUMENT_COLUMNS} FROM accounting_documents "
                "WHERE id = %s AND account_id = %s AND space_id = %s",
                (document_id, account_id, space_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            document = _row_to_dict([d[0] for d in cursor.description], row)
            cursor.execute(
                f"SELECT {_LINE_ITEM_COLUMNS} FROM accounting_line_items "
                "WHERE document_id = %s AND account_id = %s AND space_id = %s "
                "ORDER BY line_no, id",
                (document_id, account_id, space_id),
            )
            columns = [d[0] for d in cursor.description]
            document["line_items"] = [_row_to_dict(columns, line) for line in cursor.fetchall()]
        return document

    def list_documents(self, account_id: str, space_id: str, status: str = "") -> list[dict]:
        clause = "account_id = %s AND space_id = %s"
        params: list = [account_id, space_id]
        if status:
            clause += " AND status = %s"
            params.append(status)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DOCUMENT_COLUMNS} FROM accounting_documents WHERE {clause} "
                "ORDER BY created_at DESC, id DESC",
                tuple(params),
            )
            document_columns = [d[0] for d in cursor.description]
            documents = [_row_to_dict(document_columns, row) for row in cursor.fetchall()]
            if not documents:
                return []
            cursor.execute(
                f"SELECT {_LINE_ITEM_COLUMNS} FROM accounting_line_items "
                "WHERE account_id = %s AND space_id = %s AND document_id = ANY(%s) "
                "ORDER BY document_id, line_no, id",
                (account_id, space_id, [document["id"] for document in documents]),
            )
            line_columns = [d[0] for d in cursor.description]
            grouped: dict[str, list] = {}
            for line in cursor.fetchall():
                item = _row_to_dict(line_columns, line)
                grouped.setdefault(item["document_id"], []).append(item)
        for document in documents:
            document["line_items"] = grouped.get(document["id"], [])
        return documents

    def find_duplicate(
        self, account_id: str, space_id: str, dedup_key: str, *, exclude_id: str = "",
    ):
        if not dedup_key:
            return None
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s AND dedup_key = %s AND id <> %s "
                "ORDER BY created_at LIMIT 1",
                (account_id, space_id, dedup_key, exclude_id),
            )
            row = cursor.fetchone()
        return self.get_document(account_id, space_id, row[0]) if row else None

    def document_for_revision(
        self, account_id: str, space_id: str, drive_file_id: str, drive_revision_id: str,
    ):
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s "
                "AND drive_file_id = %s AND drive_revision_id = %s LIMIT 1",
                (account_id, space_id, drive_file_id, drive_revision_id),
            )
            row = cursor.fetchone()
        return self.get_document(account_id, space_id, row[0]) if row else None

    def documented_revision_ids(self, account_id: str, space_id: str) -> set[str]:
        """Revisions in this workspace that already have a document (extraction done)."""
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT drive_revision_id FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s AND COALESCE(drive_revision_id,'') <> ''",
                (account_id, space_id),
            )
            return {row[0] for row in cursor.fetchall()}

    def invoice_number_seen(
        self, account_id: str, space_id: str, issuer_name: str, invoice_number: str,
        *, exclude_id: str = "",
    ) -> bool:
        if not (invoice_number or "").strip():
            return False
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s "
                "AND lower(btrim(invoice_number)) = lower(btrim(%s)) "
                "AND lower(btrim(issuer_name)) = lower(btrim(%s)) "
                "AND id <> %s LIMIT 1",
                (account_id, space_id, invoice_number, issuer_name or "", exclude_id),
            )
            return cursor.fetchone() is not None

    # ---- writes -------------------------------------------------------------

    def create_document(self, document: dict, line_items: list[dict]) -> dict:
        account_id = document["account_id"]
        space_id = document["space_id"]
        document = {**document, "check_flags": dict(document.get("check_flags") or {})}
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"acct:{account_id}:{space_id}",),
                )
                cursor.execute(
                    "SELECT 1 FROM accounting_documents "
                    "WHERE id = %s AND account_id = %s AND space_id = %s",
                    (document["id"], account_id, space_id),
                )
                if cursor.fetchone() is None:
                    # Dedup lookup inside the same locked tx as the insert so two
                    # concurrent creates of one invoice can't both miss the duplicate.
                    self._finalize_dedup_flags(cursor, account_id, space_id, document)
                    cursor.execute(
                        f"INSERT INTO accounting_documents ({_DOCUMENT_COLUMNS}) "
                        f"VALUES ({_DOCUMENT_PLACEHOLDERS}) ON CONFLICT (id) DO NOTHING",
                        _row_params(_DOCUMENT_COLUMN_LIST, document),
                    )
                    for line in line_items:
                        cursor.execute(
                            f"INSERT INTO accounting_line_items ({_LINE_ITEM_COLUMNS}) "
                            f"VALUES ({_LINE_ITEM_PLACEHOLDERS}) ON CONFLICT (id) DO NOTHING",
                            _row_params(_LINE_ITEM_COLUMN_LIST, line),
                        )
            connection.commit()
        return self.get_document(account_id, space_id, document["id"])

    def _finalize_dedup_flags(self, cursor, account_id: str, space_id: str, document: dict) -> None:
        flags = document["check_flags"]
        dedup_key = document.get("dedup_key") or ""
        duplicate_id = ""
        if dedup_key:
            cursor.execute(
                "SELECT id FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s AND dedup_key = %s AND id <> %s "
                "ORDER BY created_at LIMIT 1",
                (account_id, space_id, dedup_key, document["id"]),
            )
            row = cursor.fetchone()
            duplicate_id = row[0] if row else ""
        flags["duplicate"] = bool(duplicate_id)
        flags["duplicate_of"] = duplicate_id
        invoice_number = (document.get("invoice_number") or "").strip()
        if invoice_number:
            cursor.execute(
                "SELECT 1 FROM accounting_documents "
                "WHERE account_id = %s AND space_id = %s "
                "AND lower(btrim(invoice_number)) = lower(btrim(%s)) "
                "AND lower(btrim(issuer_name)) = lower(btrim(%s)) AND id <> %s LIMIT 1",
                (account_id, space_id, invoice_number, document.get("issuer_name") or "", document["id"]),
            )
            flags["invoice_number_unique"] = cursor.fetchone() is None
        else:
            flags["invoice_number_unique"] = True
        flags["needs_review"] = needs_review(flags)

    def confirm_documents(
        self, account_id: str, space_id: str, confirmations: list[dict], confirmed_by: str,
    ) -> list[dict]:
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"acct:{account_id}:{space_id}",),
                )
                for confirmation in confirmations:
                    document_id = confirmation.get("document_id", "")
                    cursor.execute(
                        "SELECT id FROM accounting_documents "
                        "WHERE id = %s AND account_id = %s AND space_id = %s FOR UPDATE",
                        (document_id, account_id, space_id),
                    )
                    if cursor.fetchone() is None:
                        raise KeyError(document_id)
                    corrections = {
                        correction["id"]: correction
                        for correction in confirmation.get("line_items", [])
                        if correction.get("id")
                    }
                    cursor.execute(
                        "SELECT id, proposed_account, proposed_tax_key, cost_center "
                        "FROM accounting_line_items "
                        "WHERE document_id = %s AND account_id = %s AND space_id = %s",
                        (document_id, account_id, space_id),
                    )
                    for line_id, proposed_account, proposed_tax_key, cost_center in cursor.fetchall():
                        correction = corrections.get(line_id, {})
                        # Explicit "" clears the field; only an omitted field falls back
                        # to the proposal (so a tax-exempt line can drop its BU key).
                        if "account" in correction:
                            confirmed_account = (correction.get("account") or "").strip()
                        else:
                            confirmed_account = (proposed_account or "").strip()
                        if "tax_key" in correction:
                            confirmed_tax_key = (correction.get("tax_key") or "").strip()
                        else:
                            confirmed_tax_key = (proposed_tax_key or "").strip()
                        new_cost = (
                            (correction.get("cost_center") or "").strip()
                            if "cost_center" in correction else cost_center
                        )
                        cursor.execute(
                            "UPDATE accounting_line_items SET confirmed_account = %s, "
                            "confirmed_tax_key = %s, cost_center = %s, updated_at = now() "
                            "WHERE id = %s AND account_id = %s AND space_id = %s",
                            (confirmed_account, confirmed_tax_key, new_cost, line_id, account_id, space_id),
                        )
                    direction = confirmation.get("direction")
                    if direction in (INCOMING, OUTGOING):
                        cursor.execute(
                            "UPDATE accounting_documents SET status = 'confirmed', "
                            "confirmed_by = %s, direction = %s, updated_at = now() "
                            "WHERE id = %s AND account_id = %s AND space_id = %s",
                            (confirmed_by, direction, document_id, account_id, space_id),
                        )
                    else:
                        cursor.execute(
                            "UPDATE accounting_documents SET status = 'confirmed', "
                            "confirmed_by = %s, updated_at = now() "
                            "WHERE id = %s AND account_id = %s AND space_id = %s",
                            (confirmed_by, document_id, account_id, space_id),
                        )
            connection.commit()
        return [
            self.get_document(account_id, space_id, confirmation.get("document_id", ""))
            for confirmation in confirmations
        ]

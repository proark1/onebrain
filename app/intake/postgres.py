"""Postgres-backed intake store."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import List, Optional

import psycopg

from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema
from app.intake.base import IntakeRecord


class PostgresIntakeStore:
    def __init__(self, database_url: str, operator_dsn: str | None = None):
        self.database_url = database_url
        self._operator_dsn = operator_dsn or database_url
        self._validate_schema()

    def _conn(self, *, admin: bool = False):
        # admin connections use the privileged operator role (RLS bypass by
        # identity via _onebrain_rls_admin), never a runtime-settable flag.
        return psycopg.connect(self._operator_dsn if admin else self.database_url)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("intake_records",))

    def _row(self, r) -> IntakeRecord:
        return IntakeRecord(
            id=r[0], tenant_id=r[1], account_id=r[2], space_id=r[3], app_id=r[4], purpose=r[5],
            source=r[6], source_ref=r[7], record_type=r[8], intent=r[9], classification=r[10],
            confidence=float(r[11]), status=r[12], title=r[13], content=r[14], summary=r[15],
            extracted_facts=r[16] or {}, metadata=r[17] or {}, created_at=r[18] or "",
        )

    _COLS = (
        "id, tenant_id, account_id, space_id, app_id, purpose, source, source_ref, record_type, "
        "intent, classification, confidence, status, title, content, summary, extracted_facts, metadata, created_at"
    )

    def create(self, record: IntakeRecord) -> IntakeRecord:
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(
                conn,
                tenant_id=record.tenant_id,
                account_id=record.account_id,
                space_id=record.space_id,
            )
            cur.execute(
                """
                INSERT INTO intake_records (
                    id, tenant_id, account_id, space_id, app_id, purpose, source, source_ref,
                    record_type, intent, classification, confidence, status, title, content,
                    summary, extracted_facts, metadata, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id, record.tenant_id, record.account_id, record.space_id, record.app_id,
                    record.purpose, record.source, record.source_ref, record.record_type, record.intent,
                    record.classification, record.confidence, record.status, record.title, record.content,
                    record.summary, json.dumps(record.extracted_facts), json.dumps(record.metadata),
                    record.created_at,
                ),
            )
            conn.commit()
        return record

    def get(
        self,
        record_id: str,
        tenant_id: str = "",
        account_id: str = "",
        space_id: str = "",
    ) -> Optional[IntakeRecord]:
        with self._conn() as conn, conn.cursor() as cur:
            if tenant_id:
                set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(
                f"SELECT {self._COLS} FROM intake_records WHERE id = %s",
                (record_id,),
            )
            row = cur.fetchone()
            return self._row(row) if row else None

    def list_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[IntakeRecord]:
        clauses = ["tenant_id = %s"]
        params = [tenant_id]
        if account_id:
            clauses.append("account_id = %s")
            params.append(account_id)
        if space_id:
            clauses.append("space_id = %s")
            params.append(space_id)
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(
                f"SELECT {self._COLS} FROM intake_records WHERE {' AND '.join(clauses)} ORDER BY created_at, id",
                params,
            )
            return [self._row(row) for row in cur.fetchall()]

    def export_records(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[dict]:
        return [asdict(record) for record in self.list_by_scope(tenant_id, account_id, space_id)]

    def delete_records_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> int:
        clauses = ["tenant_id = %s"]
        params = [tenant_id]
        if account_id:
            clauses.append("account_id = %s")
            params.append(account_id)
        if space_id:
            clauses.append("space_id = %s")
            params.append(space_id)
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(f"DELETE FROM intake_records WHERE {' AND '.join(clauses)}", params)
            deleted = cur.rowcount
            conn.commit()
            return deleted

    def count(self) -> int:
        with self._conn(admin=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM intake_records")
            return int(cur.fetchone()[0])

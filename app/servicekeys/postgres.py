"""Postgres-backed service-key store. Scopes are stored comma-joined."""

from __future__ import annotations

from typing import List, Optional

from app.db.schema import validate_postgres_schema
from app.servicekeys.base import ServiceKey


class PostgresServiceKeyStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("service_keys",))

    def _row(self, r) -> ServiceKey:
        return ServiceKey(
            id=r[0], key_hash=r[1], tenant_id=r[2],
            scopes=tuple(s for s in (r[3] or "").split(",") if s),
            label=r[4], account_id=r[5], app_id=r[6],
            space_ids=tuple(s for s in (r[7] or "").split(",") if s),
            purposes=tuple(s for s in (r[8] or "").split(",") if s),
            status=r[9], created_at=r[10].isoformat() if r[10] else "",
        )

    _COLS = "id, key_hash, tenant_id, scopes, label, account_id, app_id, space_ids, purposes, status, created_at"

    def get(self, key_id: str) -> Optional[ServiceKey]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM service_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def create(self, key: ServiceKey) -> ServiceKey:
        with self._conn() as conn, conn.cursor() as cur:
            # No ON CONFLICT: a duplicate id must raise (mint then surfaces 500)
            # rather than hand the admin a plaintext for an unstored key.
            cur.execute(
                "INSERT INTO service_keys "
                "(id, key_hash, tenant_id, scopes, label, account_id, app_id, space_ids, purposes, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    key.id, key.key_hash, key.tenant_id, ",".join(key.scopes), key.label,
                    key.account_id, key.app_id, ",".join(key.space_ids), ",".join(key.purposes), key.status,
                ),
            )
            conn.commit()
        return key

    def list_by_tenant(self, tenant_id: str) -> List[ServiceKey]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM service_keys WHERE tenant_id = %s ORDER BY created_at", (tenant_id,))
            rows = cur.fetchall()
        return [self._row(r) for r in rows]

    def revoke(self, key_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE service_keys SET status = 'revoked' WHERE id = %s", (key_id,))
            changed = cur.rowcount
            conn.commit()
        return changed > 0

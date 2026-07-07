"""Postgres-backed service-key store. Scopes are stored comma-joined."""

from __future__ import annotations

from typing import List, Optional

from app.servicekeys.base import ServiceKey


class PostgresServiceKeyStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._init_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS service_keys (
                    id TEXT PRIMARY KEY,
                    key_hash TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS service_keys_tenant_idx ON service_keys (tenant_id)")
            conn.commit()

    def _row(self, r) -> ServiceKey:
        return ServiceKey(
            id=r[0], key_hash=r[1], tenant_id=r[2],
            scopes=tuple(s for s in (r[3] or "").split(",") if s),
            label=r[4], status=r[5], created_at=r[6].isoformat() if r[6] else "",
        )

    _COLS = "id, key_hash, tenant_id, scopes, label, status, created_at"

    def get(self, key_id: str) -> Optional[ServiceKey]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM service_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def create(self, key: ServiceKey) -> ServiceKey:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO service_keys (id, key_hash, tenant_id, scopes, label, status) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (key.id, key.key_hash, key.tenant_id, ",".join(key.scopes), key.label, key.status),
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

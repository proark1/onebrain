"""Postgres-backed service-key store. Scopes are stored comma-joined."""

from __future__ import annotations

from typing import List, Optional

from app.db.schema import validate_postgres_schema
from app.servicekeys.base import ServiceKey, ServiceKeySummary, sanitize_usage_endpoint


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
            last_used_at=_iso(r[11]), last_used_endpoint=r[12] or "",
            use_count=int(r[13] or 0), rotated_from_id=r[14] or "",
            revoked_at=_iso(r[15]),
        )

    _COLS = (
        "id, key_hash, tenant_id, scopes, label, account_id, app_id, space_ids, purposes, status, created_at, "
        "last_used_at, last_used_endpoint, use_count, rotated_from_id, revoked_at"
    )

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
                "(id, key_hash, tenant_id, scopes, label, account_id, app_id, space_ids, purposes, status, "
                "last_used_endpoint, use_count, rotated_from_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    key.id, key.key_hash, key.tenant_id, ",".join(key.scopes), key.label,
                    key.account_id, key.app_id, ",".join(key.space_ids), ",".join(key.purposes), key.status,
                    key.last_used_endpoint, int(key.use_count or 0), key.rotated_from_id,
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
            cur.execute(
                "UPDATE service_keys SET status = 'revoked', revoked_at = COALESCE(revoked_at, now()) WHERE id = %s",
                (key_id,),
            )
            changed = cur.rowcount
            conn.commit()
        return changed > 0

    def summary(self, tenant_id: str = "") -> ServiceKeySummary:
        clauses = []
        params = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM service_keys{where}", params)
            total = int(cur.fetchone()[0])
            cur.execute(f"SELECT status, COUNT(*) FROM service_keys{where} GROUP BY status", params)
            by_status = {str(row[0]): int(row[1]) for row in cur.fetchall()}
        return ServiceKeySummary(
            total=total,
            active=by_status.get("active", 0),
            revoked=by_status.get("revoked", 0),
        )

    def record_usage(self, key_id: str, endpoint: str) -> ServiceKey:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE service_keys
                SET last_used_at = now(),
                    last_used_endpoint = %s,
                    use_count = use_count + 1
                WHERE id = %s AND status = 'active'
                RETURNING {self._COLS}
                """,
                (sanitize_usage_endpoint(endpoint), key_id),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError(f"unknown active service key: {key_id}")
        return self._row(row)

    def rotate(self, old_key_id: str, new_key: ServiceKey) -> ServiceKey:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM service_keys WHERE id = %s FOR UPDATE", (old_key_id,))
            old_row = cur.fetchone()
            if not old_row:
                raise KeyError(f"unknown service key: {old_key_id}")
            old = self._row(old_row)
            if old.status != "active":
                raise ValueError("cannot rotate inactive service key")
            if new_key.tenant_id != old.tenant_id:
                raise ValueError("rotated service key must stay in the same tenant")
            cur.execute(
                f"""
                INSERT INTO service_keys (
                    id, key_hash, tenant_id, scopes, label, account_id, app_id, space_ids, purposes,
                    status, last_used_endpoint, use_count, rotated_from_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', '', 0, %s)
                RETURNING {self._COLS}
                """,
                (
                    new_key.id, new_key.key_hash, old.tenant_id, ",".join(old.scopes), old.label,
                    old.account_id, old.app_id, ",".join(old.space_ids), ",".join(old.purposes), old.id,
                ),
            )
            row = cur.fetchone()
            cur.execute(
                "UPDATE service_keys SET status = 'revoked', revoked_at = COALESCE(revoked_at, now()) WHERE id = %s",
                (old.id,),
            )
            conn.commit()
        return self._row(row)


def _iso(value) -> str:
    return value.isoformat() if value else ""

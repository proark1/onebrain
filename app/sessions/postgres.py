"""Postgres-backed session store (deployed app).

The table is looked up by an unguessable session id during authentication —
before any tenant context exists — so it is deliberately NOT under tenant
row-level security. It holds no business content, only session lifecycle.
"""

from __future__ import annotations

from typing import List, Optional

from app.db.schema import validate_postgres_schema
from app.sessions.base import Session


def _iso(value) -> str:
    return value.isoformat() if value else ""


class PostgresSessionStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("auth_sessions",))

    _COLS = "id, user_id, tenant_id, created_at, expires_at, revoked_at"

    def _row(self, r) -> Session:
        return Session(id=r[0], user_id=r[1], tenant_id=r[2] or "",
                       created_at=_iso(r[3]), expires_at=_iso(r[4]), revoked_at=_iso(r[5]))

    def create(self, session: Session) -> Session:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_sessions (id, user_id, tenant_id, created_at, expires_at) "
                "VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now()), %s::timestamptz)",
                (session.id, session.user_id, session.tenant_id,
                 session.created_at or None, session.expires_at or None),
            )
            conn.commit()
        return session

    def get(self, session_id: str) -> Optional[Session]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM auth_sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def revoke(self, session_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, now()) "
                "WHERE id = %s AND revoked_at IS NULL",
                (session_id,),
            )
            changed = cur.rowcount
            conn.commit()
        return changed > 0

    def revoke_all_for_user(self, user_id: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_sessions SET revoked_at = now() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
            changed = cur.rowcount
            conn.commit()
        return int(changed)

    def list_for_user(self, user_id: str) -> List[Session]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLS} FROM auth_sessions WHERE user_id = %s ORDER BY created_at",
                (user_id,),
            )
            rows = cur.fetchall()
        return [self._row(r) for r in rows]

    def purge_expired(self, now_iso: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM auth_sessions WHERE expires_at < %s::timestamptz", (now_iso,))
            changed = cur.rowcount
            conn.commit()
        return int(changed)

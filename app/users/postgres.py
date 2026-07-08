"""Postgres-backed user store (deployed app)."""

from __future__ import annotations

from typing import List, Optional

from app.db.schema import validate_postgres_schema
from app.users.base import User


class PostgresUserStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("users",))

    def _row(self, r) -> User:
        return User(id=r[0], email=r[1], display_name=r[2], password_hash=r[3],
                    tenant_id=r[4], role_id=r[5], location=r[6], status=r[7],
                    created_at=r[8].isoformat() if r[8] else "")

    _COLS = "id, email, display_name, password_hash, tenant_id, role_id, location, status, created_at"

    def get(self, user_id: str) -> Optional[User]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def get_by_email(self, email: str) -> Optional[User]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM users WHERE email = %s", (email.strip().lower(),))
            row = cur.fetchone()
        return self._row(row) if row else None

    def create(self, user: User) -> User:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name, password_hash, tenant_id, role_id, location, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
                (user.id, user.email.strip().lower(), user.display_name, user.password_hash,
                 user.tenant_id, user.role_id, user.location, user.status),
            )
            conn.commit()
        return user

    def delete_by_email(self, email: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email = %s", (email.strip().lower(),))
            deleted = cur.rowcount
            conn.commit()
        return deleted > 0

    def count(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            return int(cur.fetchone()[0])

    def list_by_tenant(self, tenant_id: str) -> List[User]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM users WHERE tenant_id = %s ORDER BY email", (tenant_id,))
            rows = cur.fetchall()
        return [self._row(r) for r in rows]

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
                    created_at=r[8].isoformat() if r[8] else "",
                    must_change_password=bool(r[9]))

    _COLS = ("id, email, display_name, password_hash, tenant_id, role_id, location, status, "
             "created_at, must_change_password")

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
                "INSERT INTO users (id, email, display_name, password_hash, tenant_id, role_id, location, "
                "status, must_change_password) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
                (user.id, user.email.strip().lower(), user.display_name, user.password_hash,
                 user.tenant_id, user.role_id, user.location, user.status, user.must_change_password),
            )
            conn.commit()
        return user

    def update_password(self, user_id: str, password_hash: str, *, must_change_password: bool) -> User:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE users SET password_hash = %s, must_change_password = %s WHERE id = %s "
                f"RETURNING {self._COLS}",
                (password_hash, must_change_password, user_id),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError(f"unknown user: {user_id}")
        return self._row(row)

    def update_scope(self, user_id: str, *, tenant_id: str, role_id: str, location: str) -> User:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE users SET tenant_id = %s, role_id = %s, location = %s WHERE id = %s "
                f"RETURNING {self._COLS}",
                (tenant_id, role_id, location, user_id),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError(f"unknown user: {user_id}")
        return self._row(row)

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

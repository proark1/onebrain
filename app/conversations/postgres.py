"""Postgres-backed conversation store (deployed app)."""

from __future__ import annotations

import json
import uuid
from typing import List, Optional

from app.conversations.base import Conversation, Message, Scope


def _iso(ts) -> str:
    return ts.isoformat() if ts is not None else ""


class PostgresConversationStore:
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
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    account_id TEXT NOT NULL DEFAULT '',
                    space_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS space_id TEXT NOT NULL DEFAULT ''")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS conv_scope_idx "
                "ON conversations (tenant_id, session_id, role_id, account_id, space_id, updated_at DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS conv_scope_space_idx "
                "ON conversations (tenant_id, session_id, role_id, account_id, space_id, updated_at DESC)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS msg_conv_idx ON messages (conversation_id, created_at)")
            conn.commit()

    def create(self, scope: Scope, title: str) -> Conversation:
        cid = uuid.uuid4().hex
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, tenant_id, session_id, role_id, title, account_id, space_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING created_at, updated_at",
                (
                    cid, scope.tenant_id, scope.session_id, scope.role_id,
                    (title or "New chat")[:80], scope.account_id, scope.space_id,
                ),
            )
            created, updated = cur.fetchone()
            conn.commit()
        return Conversation(cid, scope.tenant_id, scope.session_id, scope.role_id,
                            (title or "New chat")[:80], _iso(created), _iso(updated),
                            scope.account_id, scope.space_id)

    def get(self, conversation_id: str, scope: Scope) -> Optional[Conversation]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE id = %s AND tenant_id = %s AND session_id = %s AND role_id = %s "
                "AND account_id = %s AND space_id = %s",
                (
                    conversation_id, scope.tenant_id, scope.session_id, scope.role_id,
                    scope.account_id, scope.space_id,
                ),
            )
            row = cur.fetchone()
        if not row:
            return None
        return Conversation(row[0], scope.tenant_id, scope.session_id, scope.role_id,
                            row[1], _iso(row[2]), _iso(row[3]), scope.account_id, scope.space_id)

    def list(self, scope: Scope, limit: int = 50) -> List[Conversation]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE tenant_id = %s AND session_id = %s AND role_id = %s "
                "AND account_id = %s AND space_id = %s "
                "ORDER BY updated_at DESC LIMIT %s",
                (scope.tenant_id, scope.session_id, scope.role_id, scope.account_id, scope.space_id, limit),
            )
            rows = cur.fetchall()
        return [Conversation(
                    r[0], scope.tenant_id, scope.session_id, scope.role_id, r[1], _iso(r[2]), _iso(r[3]),
                    scope.account_id, scope.space_id,
                )
                for r in rows]

    def add_message(self, conversation_id: str, role: str, content: str, meta: Optional[dict] = None) -> Message:
        mid = uuid.uuid4().hex
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (id, conversation_id, role, content, meta) VALUES (%s, %s, %s, %s, %s) "
                "RETURNING created_at",
                (mid, conversation_id, role, content, json.dumps(meta) if meta else None),
            )
            created = cur.fetchone()[0]
            cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
            conn.commit()
        return Message(role=role, content=content, id=mid, created_at=_iso(created), meta=meta or {})

    def get_messages(self, conversation_id: str, limit: Optional[int] = None) -> List[Message]:
        sql = "SELECT id, role, content, meta, created_at FROM messages WHERE conversation_id = %s ORDER BY created_at"
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (conversation_id,))
            rows = cur.fetchall()
        msgs = [Message(role=r[1], content=r[2], id=r[0], created_at=_iso(r[4]), meta=r[3] or {}) for r in rows]
        return msgs[-limit:] if limit else msgs

    def delete(self, conversation_id: str, scope: Scope) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE id = %s AND tenant_id = %s AND session_id = %s AND role_id = %s "
                "AND account_id = %s AND space_id = %s",
                (
                    conversation_id, scope.tenant_id, scope.session_id, scope.role_id,
                    scope.account_id, scope.space_id,
                ),
            )
            removed = cur.rowcount
            conn.commit()
        return removed > 0

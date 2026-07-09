"""Postgres-backed conversation store (deployed app)."""

from __future__ import annotations

import json
import uuid
from typing import List, Optional

from app.conversations.base import Conversation, Message, Scope
from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema


def _iso(ts) -> str:
    return ts.isoformat() if ts is not None else ""


def _privacy_where(tenant_id: str, account_id: str = "", space_id: str = "") -> tuple[str, list]:
    clauses = ["tenant_id = %s"]
    params: list = [tenant_id]
    if space_id:
        clauses.append("account_id = %s")
        clauses.append("space_id = %s")
        params.extend([account_id, space_id])
    elif account_id:
        clauses.append("account_id = ANY(%s)")
        params.append(["", account_id])
    return " AND ".join(clauses), params


class PostgresConversationStore:
    def __init__(self, dsn: str, operator_dsn: str | None = None):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._operator_dsn = operator_dsn or dsn
        self._validate_schema()

    def _conn(self, *, admin: bool = False):
        # admin connections use the privileged operator role, which bypasses RLS
        # by identity (see _onebrain_rls_admin) — no runtime-settable flag.
        return self._psycopg.connect(self._operator_dsn if admin else self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("conversations", "messages"))

    def create(self, scope: Scope, title: str) -> Conversation:
        cid = uuid.uuid4().hex
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=scope.tenant_id, account_id=scope.account_id, space_id=scope.space_id)
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
            set_rls_scope(conn, tenant_id=scope.tenant_id, account_id=scope.account_id, space_id=scope.space_id)
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
            set_rls_scope(conn, tenant_id=scope.tenant_id, account_id=scope.account_id, space_id=scope.space_id)
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

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        meta: Optional[dict] = None,
        scope: Optional[Scope] = None,
    ) -> Message:
        mid = uuid.uuid4().hex
        with self._conn(admin=scope is None) as conn, conn.cursor() as cur:
            if scope:
                set_rls_scope(conn, tenant_id=scope.tenant_id, account_id=scope.account_id, space_id=scope.space_id)
            cur.execute(
                "INSERT INTO messages (id, conversation_id, role, content, meta) VALUES (%s, %s, %s, %s, %s) "
                "RETURNING created_at",
                (mid, conversation_id, role, content, json.dumps(meta) if meta else None),
            )
            created = cur.fetchone()[0]
            cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
            conn.commit()
        return Message(role=role, content=content, id=mid, created_at=_iso(created), meta=meta or {})

    def get_messages(
        self,
        conversation_id: str,
        limit: Optional[int] = None,
        scope: Optional[Scope] = None,
    ) -> List[Message]:
        sql = "SELECT id, role, content, meta, created_at FROM messages WHERE conversation_id = %s ORDER BY created_at"
        with self._conn(admin=scope is None) as conn, conn.cursor() as cur:
            if scope:
                set_rls_scope(conn, tenant_id=scope.tenant_id, account_id=scope.account_id, space_id=scope.space_id)
            cur.execute(sql, (conversation_id,))
            rows = cur.fetchall()
        msgs = [Message(role=r[1], content=r[2], id=r[0], created_at=_iso(r[4]), meta=r[3] or {}) for r in rows]
        return msgs[-limit:] if limit else msgs

    def delete(self, conversation_id: str, scope: Scope) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=scope.tenant_id, account_id=scope.account_id, space_id=scope.space_id)
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

    def export_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[dict]:
        where, params = _privacy_where(tenant_id, account_id, space_id)
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(
                "SELECT id, tenant_id, account_id, space_id, session_id, role_id, title, created_at, updated_at "
                f"FROM conversations WHERE {where} ORDER BY updated_at DESC",
                params,
            )
            conversations = cur.fetchall()
            ids = [row[0] for row in conversations]
            messages_by_conv: dict[str, list] = {conversation_id: [] for conversation_id in ids}
            if ids:
                cur.execute(
                    "SELECT conversation_id, id, role, content, meta, created_at FROM messages "
                    "WHERE conversation_id = ANY(%s) ORDER BY created_at",
                    (ids,),
                )
                for conversation_id, msg_id, role, content, meta, created_at in cur.fetchall():
                    messages_by_conv.setdefault(conversation_id, []).append({
                        "id": msg_id,
                        "role": role,
                        "content": content,
                        "created_at": _iso(created_at),
                        "meta": meta or {},
                    })
        return [
            {
                "id": row[0],
                "tenant_id": row[1],
                "account_id": row[2],
                "space_id": row[3],
                "session_id": row[4],
                "role_id": row[5],
                "title": row[6],
                "created_at": _iso(row[7]),
                "updated_at": _iso(row[8]),
                "messages": messages_by_conv.get(row[0], []),
            }
            for row in conversations
        ]

    def delete_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> int:
        where, params = _privacy_where(tenant_id, account_id, space_id)
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(f"DELETE FROM conversations WHERE {where}", params)
            removed = cur.rowcount
            conn.commit()
        return removed

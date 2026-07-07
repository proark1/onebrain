"""Postgres + pgvector store — the production target.

Requires `pip install "psycopg[binary]" pgvector` and a Postgres with the
`vector` extension. The access filter is compiled into the SQL WHERE clause, so
the database engine itself enforces the boundary (see `AccessFilter.to_sql`).
Kept import-light so the base app doesn't depend on it.
"""

from __future__ import annotations

import json
from typing import List

import numpy as np

from app.security.policy import AccessFilter
from app.store.base import Chunk, Hit


class PgVectorStore:
    def __init__(self, dsn: str, dim: int):
        import psycopg
        from pgvector.psycopg import register_vector

        self._psycopg = psycopg
        self._register_vector = register_vector
        self._dsn = dsn
        self._dim = dim
        self._init_schema()

    def _conn(self):
        conn = self._psycopg.connect(self._dsn)
        self._register_vector(conn)
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            # If a chunks table already exists with a different embedding
            # dimension (i.e. the embedding model changed), drop it — vectors
            # from two models aren't comparable, so the index must be rebuilt.
            # The app re-seeds automatically when the store is empty.
            cur.execute(
                "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c "
                "ON c.oid = a.attrelid WHERE c.relname = 'chunks' AND a.attname = 'embedding'"
            )
            existing = cur.fetchone()
            if existing is not None and existing[0] > 0 and existing[0] != self._dim:
                cur.execute("DROP TABLE IF EXISTS chunks")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    meta JSONB NOT NULL,
                    embedding vector({self._dim}),
                    tenant_id TEXT
                )
                """
            )
            # Idempotent for tables created before the tenant column existed.
            # (The isolation guarantee is enforced via meta->>'tenant_id' in
            # AccessFilter.to_sql; this column backs the Phase-1 RLS backstop.)
            cur.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tenant_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS chunks_doc_id_idx ON chunks (doc_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON chunks (tenant_id)")
            conn.commit()

    def add(self, chunks: List[Chunk]) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            for c in chunks:
                cur.execute(
                    "INSERT INTO chunks (id, doc_id, text, meta, embedding, tenant_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (c.id, c.doc_id, c.text, json.dumps(c.meta), np.asarray(c.embedding),
                     c.meta.get("tenant_id")),
                )
            conn.commit()

    def search(self, query: np.ndarray, k: int, access: AccessFilter) -> List[Hit]:
        where, params = access.to_sql()
        sql = (
            "SELECT id, doc_id, text, meta, 1 - (embedding <=> %s) AS score "
            f"FROM chunks WHERE {where} ORDER BY embedding <=> %s LIMIT %s"
        )
        args = [np.asarray(query), *params, np.asarray(query), k]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [
            Hit(chunk=Chunk(id=r[0], doc_id=r[1], text=r[2], meta=r[3]), score=float(r[4]))
            for r in rows
        ]

    def list_documents(self, access: AccessFilter) -> List[dict]:
        where, params = access.to_sql()
        sql = (
            "SELECT doc_id, meta->>'doc_title', meta->>'classification_label', "
            "meta->>'location', meta->>'category', count(*) "
            f"FROM chunks WHERE {where} GROUP BY 1, 2, 3, 4, 5 ORDER BY 2"
        )
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {"doc_id": r[0], "title": r[1] or "Untitled", "classification": r[2] or "internal",
             "location": r[3] or "global", "category": r[4] or "general", "chunks": r[5]}
            for r in rows
        ]

    def delete_document(self, doc_id: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            removed = cur.rowcount
            conn.commit()
        return removed

    def count(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks")
            return int(cur.fetchone()[0])

"""Pick the vector store from config."""

from __future__ import annotations

import os

from app.config import Settings


def build_store(settings: Settings, dim: int):
    if settings.vector_store == "pgvector":
        from app.store.pgvector import PgVectorStore

        return PgVectorStore(settings.pg_database_url, dim, operator_dsn=settings.pg_operator_database_url)

    from app.store.memory import MemoryStore

    path = os.path.join(settings.data_dir, "store.pkl") if settings.persist else None
    return MemoryStore(persist_path=path)

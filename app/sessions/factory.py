"""Pick the session store — Postgres when the app runs on pgvector, else memory."""

from __future__ import annotations

import os

from app.config import Settings


def build_session_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.sessions.postgres import PostgresSessionStore

        return PostgresSessionStore(settings.pg_database_url)

    from app.sessions.memory import MemorySessionStore

    path = os.path.join(settings.data_dir, "sessions.pkl") if settings.persist else None
    return MemorySessionStore(persist_path=path)

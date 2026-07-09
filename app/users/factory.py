"""Pick the user store — Postgres when the app runs on pgvector, else memory."""

from __future__ import annotations

import os

from app.config import Settings


def build_user_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.users.postgres import PostgresUserStore

        return PostgresUserStore(settings.pg_database_url)

    from app.users.memory import MemoryUserStore

    path = os.path.join(settings.data_dir, "users.pkl") if settings.persist else None
    return MemoryUserStore(persist_path=path)

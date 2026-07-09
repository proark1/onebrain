"""Pick the service-key store — Postgres on pgvector, else memory."""

from __future__ import annotations

import os

from app.config import Settings


def build_service_key_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.servicekeys.postgres import PostgresServiceKeyStore

        return PostgresServiceKeyStore(settings.pg_database_url)

    from app.servicekeys.memory import MemoryServiceKeyStore

    path = os.path.join(settings.data_dir, "service_keys.json") if settings.persist else None
    return MemoryServiceKeyStore(persist_path=path)

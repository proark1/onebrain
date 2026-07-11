"""Pick the fleet store — Postgres when on pgvector, else a JSON-file memory store."""

from __future__ import annotations

import os

from app.config import Settings


def build_fleet_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.fleet.postgres import PostgresFleetStore

        return PostgresFleetStore(settings.pg_database_url)

    from app.fleet.memory import MemoryFleetStore

    path = os.path.join(settings.data_dir, "fleet.json") if settings.persist else None
    return MemoryFleetStore(persist_path=path)

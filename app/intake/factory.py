"""Build the intake store from runtime settings."""

from __future__ import annotations

import os

from app.config import Settings


def build_intake_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.intake.postgres import PostgresIntakeStore

        return PostgresIntakeStore(settings.database_url)

    from app.intake.memory import MemoryIntakeStore

    path = os.path.join(settings.data_dir, "intake_records.json") if settings.persist else None
    return MemoryIntakeStore(persist_path=path)

"""Build the platform store."""

from __future__ import annotations

import os

from app.config import Settings


def build_platform_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.platform.postgres import PostgresPlatformStore

        return PostgresPlatformStore(settings.database_url)

    from app.platform.memory import MemoryPlatformStore

    path = os.path.join(settings.data_dir, "platform.json") if settings.persist else None
    return MemoryPlatformStore(persist_path=path)

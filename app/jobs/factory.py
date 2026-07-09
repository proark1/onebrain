"""Build the configured job store."""

from __future__ import annotations

from app.config import Settings


def build_job_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.jobs.postgres import PostgresJobStore

        return PostgresJobStore(settings.pg_database_url)

    from app.jobs.memory import MemoryJobStore

    return MemoryJobStore()

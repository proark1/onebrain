"""Build the configured job store."""

from __future__ import annotations

from app.config import Settings


def build_job_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.jobs.postgres import PostgresJobStore

        # The API DSN remains tenant-scoped.  Only a worker container receives
        # the separate queue-worker DSN, and only an operator surface receives
        # the operator DSN for aggregate queue health.
        return PostgresJobStore(
            settings.pg_database_url,
            worker_dsn=settings.pg_worker_database_url,
            operator_dsn=settings.pg_operator_database_url,
        )

    from app.jobs.memory import MemoryJobStore

    return MemoryJobStore()

"""Build the accounting store selected by runtime configuration."""

from __future__ import annotations

import os

from app.config import Settings


def build_accounting_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.accounting.postgres import PostgresAccountingStore

        return PostgresAccountingStore(
            settings.pg_database_url,
            operator_dsn=settings.pg_operator_database_url,
        )

    from app.accounting.memory import MemoryAccountingStore

    path = os.path.join(settings.data_dir, "accounting.json") if settings.persist else None
    return MemoryAccountingStore(persist_path=path)

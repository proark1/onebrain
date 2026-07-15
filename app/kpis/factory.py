"""Build the KPI store selected by runtime configuration."""

from __future__ import annotations

import os

from app.config import Settings


def build_kpi_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.kpis.postgres import PostgresKpiStore

        return PostgresKpiStore(
            settings.pg_database_url,
            operator_dsn=settings.pg_operator_database_url,
        )

    from app.kpis.memory import MemoryKpiStore

    path = os.path.join(settings.data_dir, "kpis.json") if settings.persist else None
    return MemoryKpiStore(persist_path=path)

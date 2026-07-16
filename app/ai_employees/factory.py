"""Build the AI Employees store selected by runtime configuration."""

from __future__ import annotations

import os

from app.config import Settings


def build_ai_employee_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.ai_employees.postgres import PostgresAiEmployeeStore

        return PostgresAiEmployeeStore(
            settings.pg_database_url,
            operator_dsn=settings.pg_operator_database_url,
        )

    from app.ai_employees.memory import MemoryAiEmployeeStore

    path = os.path.join(settings.data_dir, "ai_employees.json") if settings.persist else None
    return MemoryAiEmployeeStore(persist_path=path)

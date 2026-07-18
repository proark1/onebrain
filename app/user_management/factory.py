"""Build MC job and customer receipt stores from the configured backend."""

from __future__ import annotations

from app.config import Settings


def build_user_management_job_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.user_management.postgres import PostgresUserManagementJobStore

        return PostgresUserManagementJobStore(settings.pg_database_url)
    from app.user_management.memory import MemoryUserManagementJobStore

    return MemoryUserManagementJobStore()


def build_user_management_receipt_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.user_management.postgres import PostgresUserManagementReceiptStore

        return PostgresUserManagementReceiptStore(settings.pg_database_url)
    from app.user_management.memory import MemoryUserManagementReceiptStore

    return MemoryUserManagementReceiptStore()

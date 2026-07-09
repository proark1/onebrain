"""Postgres row-level-security enforcement checks."""

from __future__ import annotations


RLS_REQUIRED_TABLES = (
    "chunks",
    "conversations",
    "messages",
    "intake_records",
    "platform_accounts",
    "platform_spaces",
    "platform_app_installations",
    "platform_audit_events",
    "platform_data_access_events",
)


class PostgresRLSError(RuntimeError):
    """Raised when RLS is required but not enabled for customer-scoped tables."""


def validate_rls_enabled(conn, tables=RLS_REQUIRED_TABLES) -> None:
    missing: list[str] = []
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(
                """
                SELECT relrowsecurity
                FROM pg_class
                WHERE oid = to_regclass(%s)
                """,
                (table,),
            )
            row = cur.fetchone()
            if not row or row[0] is not True:
                missing.append(table)
    if missing:
        raise PostgresRLSError(
            "Postgres RLS is required but not enabled for: " + ", ".join(missing)
        )

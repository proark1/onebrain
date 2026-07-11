"""Postgres schema validation helpers."""

from __future__ import annotations

from typing import Iterable


BASELINE_ALEMBIC_REVISION = "0001_baseline_onebrain_schema"
REQUIRED_ALEMBIC_REVISION = "0018_fleet_rollouts"
MIGRATION_GUIDANCE = (
    "Postgres schema is not migrated. Run `alembic upgrade head` with "
    "ONEBRAIN_DATABASE_URL before starting OneBrain."
)


class PostgresSchemaError(RuntimeError):
    """Raised when a Postgres-backed store sees an unmigrated database."""


def validate_postgres_schema(conn, required_tables: Iterable[str]) -> None:
    """Require the Alembic baseline and expected tables before using Postgres."""

    revision = _read_alembic_revision(conn)
    if revision != REQUIRED_ALEMBIC_REVISION:
        raise PostgresSchemaError(
            f"{MIGRATION_GUIDANCE} Current revision: {revision or 'none'}; "
            f"required revision: {REQUIRED_ALEMBIC_REVISION}."
        )

    missing = _missing_tables(conn, required_tables)
    if missing:
        raise PostgresSchemaError(
            f"{MIGRATION_GUIDANCE} Missing tables: {', '.join(missing)}."
        )


def _read_alembic_revision(conn) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
    except Exception as exc:
        raise PostgresSchemaError(MIGRATION_GUIDANCE) from exc
    return str(row[0]) if row and row[0] else ""


def _missing_tables(conn, required_tables: Iterable[str]) -> list[str]:
    missing: list[str] = []
    with conn.cursor() as cur:
        for table in required_tables:
            cur.execute("SELECT to_regclass(%s)", (table,))
            row = cur.fetchone()
            if not row or row[0] is None:
                missing.append(table)
    return missing

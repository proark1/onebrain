"""Postgres schema validation helpers."""

from __future__ import annotations

from typing import Iterable


BASELINE_ALEMBIC_REVISION = "0001_baseline_onebrain_schema"
REQUIRED_ALEMBIC_REVISION = "0029_job_queue_rls_roles"
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


def read_live_alembic_revision(dsn: str) -> str:
    """The revision actually stamped in the database (SELECT version_num FROM
    alembic_version) — for the ground-truth heartbeat, NOT for validation.
    One cheap query; caller handles/should isolate failures."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        return _read_alembic_revision(conn)

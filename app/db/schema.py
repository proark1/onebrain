"""Postgres schema validation helpers."""

from __future__ import annotations

from typing import Iterable


BASELINE_ALEMBIC_REVISION = "0001_baseline_onebrain_schema"
REQUIRED_ALEMBIC_REVISION = "0035_fleet_decommission_tombstone"
DRIVE_MALWARE_POLICY_EPOCH = 1
# The Alembic revision that OWNS the Drive-malware quarantine schema + policy
# identity. Deliberately NOT the moving head: migration 0034 stamps
# drive_malware_activation_state.schema_revision with this fixed revision, so
# advancing REQUIRED_ALEMBIC_REVISION with an UNRELATED migration (e.g. the fleet
# tombstone in 0035) must not invalidate a still-correct malware activation. Bump
# this ONLY when a migration actually changes the malware schema/policy (and
# re-stamps the activation row).
DRIVE_MALWARE_SCHEMA_REVISION = "0034_drive_malware_quarantine"
MIGRATION_GUIDANCE = (
    "Postgres schema is not migrated. Run `alembic upgrade head` with "
    "ONEBRAIN_DATABASE_URL before starting OneBrain."
)


class PostgresSchemaError(RuntimeError):
    """Raised when a Postgres-backed store sees an unmigrated database."""


def validate_postgres_schema(
    conn, required_tables: Iterable[str], *, require_malware_active: bool = False,
) -> None:
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
    if require_malware_active:
        _validate_drive_malware_activation(conn)


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


def _validate_drive_malware_activation(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schema_revision, policy_epoch, state "
                "FROM drive_malware_activation_state WHERE singleton_id=true"
            )
            row = cur.fetchone()
    except Exception as exc:
        raise PostgresSchemaError(
            "Drive malware enforcement activation is unavailable; keep API and workers stopped."
        ) from exc
    if not row or (
        str(row[0]) != DRIVE_MALWARE_SCHEMA_REVISION
        or int(row[1]) != DRIVE_MALWARE_POLICY_EPOCH
        or str(row[2]) != "active"
    ):
        state = str(row[2]) if row and len(row) > 2 else "missing"
        raise PostgresSchemaError(
            "Drive malware enforcement is not active for the required schema/policy epoch. "
            f"Current activation state: {state}. Run drive-malware-activate while services remain stopped."
        )


def read_live_alembic_revision(dsn: str) -> str:
    """The revision actually stamped in the database (SELECT version_num FROM
    alembic_version) — for the ground-truth heartbeat, NOT for validation.
    One cheap query; caller handles/should isolate failures."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        return _read_alembic_revision(conn)

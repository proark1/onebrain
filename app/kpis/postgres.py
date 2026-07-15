"""Postgres-backed KPI store with account/space RLS scoping."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema
from app.kpis.base import (
    MAX_ACTIVE_DEFINITIONS_PER_SPACE,
    MAX_BATCH_SIZE,
    MAX_DEFINITIONS_PER_SPACE,
    KpiConflictError,
    KpiDefinition,
    KpiIngestResult,
    KpiLimitError,
    KpiSeries,
    KpiSnapshot,
    bounded_history_limit,
    snapshot_semantically_equal,
    validate_definition,
    validate_snapshot,
)
from app.kpis.memory import definition_to_dict, snapshot_to_dict


_DEFINITION_COLUMNS = (
    "id, account_id, space_id, key, name, description, category, unit, "
    "source_label, owner_label, freshness_minutes, warning_min, warning_max, "
    "critical_min, critical_max, display_order, status, created_at, updated_at"
)
_SNAPSHOT_COLUMNS = (
    "id, account_id, space_id, kpi_id, value, observed_at, received_at, "
    "source_ref, idempotency_key, created_by"
)


def _iso(value) -> str:
    return value.isoformat() if value else ""


class PostgresKpiStore:
    def __init__(self, dsn: str, operator_dsn: str | None = None):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._operator_dsn = operator_dsn or dsn
        self._validate_schema()

    def _conn(self, *, account_id: str = "", space_id: str = "", admin: bool = False):
        connection = self._psycopg.connect(self._operator_dsn if admin else self._dsn)
        if account_id or space_id:
            set_rls_scope(connection, account_id=account_id, space_id=space_id)
        return connection

    def _validate_schema(self) -> None:
        with self._conn() as connection:
            validate_postgres_schema(connection, ("kpi_definitions", "kpi_snapshots"))

    @staticmethod
    def _definition_row(row) -> KpiDefinition:
        return KpiDefinition(
            id=row[0], account_id=row[1], space_id=row[2], key=row[3], name=row[4],
            description=row[5], category=row[6], unit=row[7], source_label=row[8],
            owner_label=row[9], freshness_minutes=row[10], warning_min=row[11],
            warning_max=row[12], critical_min=row[13], critical_max=row[14],
            display_order=row[15], status=row[16], created_at=_iso(row[17]), updated_at=_iso(row[18]),
        )

    @staticmethod
    def _snapshot_row(row) -> KpiSnapshot:
        return KpiSnapshot(
            id=row[0], account_id=row[1], space_id=row[2], kpi_id=row[3],
            value=Decimal(str(row[4])), observed_at=_iso(row[5]), received_at=_iso(row[6]),
            source_ref=row[7], idempotency_key=row[8], created_by=row[9],
        )

    def create_definition(self, definition: KpiDefinition) -> KpiDefinition:
        validate_definition(definition)
        with self._conn(account_id=definition.account_id, space_id=definition.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, definition.account_id, definition.space_id)
                self._check_definition_limits(cursor, definition.account_id, definition.space_id, definition.status)
                try:
                    cursor.execute(
                        f"""
                        INSERT INTO kpi_definitions
                        (id, account_id, space_id, key, name, description, category, unit,
                         source_label, owner_label, freshness_minutes, warning_min, warning_max,
                         critical_min, critical_max, display_order, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING {_DEFINITION_COLUMNS}
                        """,
                        self._definition_values(definition),
                    )
                    row = cursor.fetchone()
                except self._psycopg.errors.UniqueViolation as exc:
                    raise KpiConflictError("KPI id or key already exists in this space.") from exc
            connection.commit()
        return self._definition_row(row)

    def update_definition(self, definition: KpiDefinition) -> KpiDefinition:
        validate_definition(definition)
        with self._conn(account_id=definition.account_id, space_id=definition.space_id) as connection:
            with connection.cursor() as cursor:
                self._lock_scope(cursor, definition.account_id, definition.space_id)
                cursor.execute(
                    "SELECT status FROM kpi_definitions WHERE id = %s AND account_id = %s AND space_id = %s",
                    (definition.id, definition.account_id, definition.space_id),
                )
                current = cursor.fetchone()
                if not current:
                    raise KeyError(f"Unknown KPI definition: {definition.id}")
                if current[0] != "active" and definition.status == "active":
                    self._check_definition_limits(
                        cursor, definition.account_id, definition.space_id, definition.status,
                        excluding_id=definition.id,
                    )
                try:
                    cursor.execute(
                        f"""
                        UPDATE kpi_definitions SET
                            key = %s, name = %s, description = %s, category = %s, unit = %s,
                            source_label = %s, owner_label = %s, freshness_minutes = %s,
                            warning_min = %s, warning_max = %s, critical_min = %s, critical_max = %s,
                            display_order = %s, status = %s, updated_at = now()
                        WHERE id = %s AND account_id = %s AND space_id = %s
                        RETURNING {_DEFINITION_COLUMNS}
                        """,
                        (
                            definition.key, definition.name, definition.description,
                            definition.category, definition.unit, definition.source_label,
                            definition.owner_label, definition.freshness_minutes,
                            definition.warning_min, definition.warning_max,
                            definition.critical_min, definition.critical_max,
                            definition.display_order, definition.status, definition.id,
                            definition.account_id, definition.space_id,
                        ),
                    )
                    row = cursor.fetchone()
                except self._psycopg.errors.UniqueViolation as exc:
                    raise KpiConflictError("KPI key already exists in this space.") from exc
            connection.commit()
        return self._definition_row(row)

    def get_definition(
        self, kpi_id: str, *, account_id: str, space_id: str,
    ) -> Optional[KpiDefinition]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definitions "
                "WHERE id = %s AND account_id = %s AND space_id = %s",
                (kpi_id, account_id, space_id),
            )
            row = cursor.fetchone()
        return self._definition_row(row) if row else None

    def get_definition_by_key(
        self, key: str, *, account_id: str, space_id: str,
    ) -> Optional[KpiDefinition]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definitions "
                "WHERE key = %s AND account_id = %s AND space_id = %s",
                (key, account_id, space_id),
            )
            row = cursor.fetchone()
        return self._definition_row(row) if row else None

    def list_definitions(
        self, account_id: str, space_id: str, *, include_archived: bool = False,
    ) -> list[KpiDefinition]:
        archived_clause = "" if include_archived else "AND status = 'active'"
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definitions "
                f"WHERE account_id = %s AND space_id = %s {archived_clause} "
                "ORDER BY display_order, lower(name), id",
                (account_id, space_id),
            )
            rows = cursor.fetchall()
        return [self._definition_row(row) for row in rows]

    def ingest_snapshots(self, snapshots: Sequence[KpiSnapshot]) -> KpiIngestResult:
        rows = list(snapshots)
        if not 1 <= len(rows) <= MAX_BATCH_SIZE:
            raise KpiLimitError(f"A snapshot batch must contain 1 to {MAX_BATCH_SIZE} items.")
        for row in rows:
            validate_snapshot(row)
        account_ids = {row.account_id for row in rows}
        space_ids = {row.space_id for row in rows}
        if len(account_ids) != 1 or len(space_ids) != 1:
            raise ValueError("A snapshot batch must use one account and one space.")
        account_id = next(iter(account_ids))
        space_id = next(iter(space_ids))

        accepted = 0
        duplicates = 0
        resolved: list[KpiSnapshot] = []
        with self._conn(account_id=account_id, space_id=space_id) as connection:
            with connection.cursor() as cursor:
                for snapshot in rows:
                    cursor.execute(
                        "SELECT 1 FROM kpi_definitions WHERE id = %s AND account_id = %s AND space_id = %s",
                        (snapshot.kpi_id, account_id, space_id),
                    )
                    if not cursor.fetchone():
                        raise ValueError("KPI definition is not in the authorized account and space.")
                    cursor.execute(
                        f"""
                        INSERT INTO kpi_snapshots
                        (id, account_id, space_id, kpi_id, value, observed_at, received_at,
                         source_ref, idempotency_key, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING {_SNAPSHOT_COLUMNS}
                        """,
                        (
                            snapshot.id, snapshot.account_id, snapshot.space_id, snapshot.kpi_id,
                            snapshot.value, snapshot.observed_at, snapshot.received_at,
                            snapshot.source_ref, snapshot.idempotency_key, snapshot.created_by,
                        ),
                    )
                    stored = cursor.fetchone()
                    if stored:
                        accepted += 1
                        resolved.append(self._snapshot_row(stored))
                        continue
                    cursor.execute(
                        f"""
                        SELECT {_SNAPSHOT_COLUMNS} FROM kpi_snapshots
                        WHERE (account_id = %s AND idempotency_key = %s)
                           OR (kpi_id = %s AND observed_at = %s)
                        ORDER BY id LIMIT 1
                        """,
                        (account_id, snapshot.idempotency_key, snapshot.kpi_id, snapshot.observed_at),
                    )
                    existing_row = cursor.fetchone()
                    if not existing_row:
                        raise KpiConflictError("Snapshot conflicts with stored data.")
                    existing = self._snapshot_row(existing_row)
                    if not snapshot_semantically_equal(existing, snapshot):
                        raise KpiConflictError("Idempotency key or KPI observation conflicts with stored data.")
                    duplicates += 1
                    resolved.append(existing)
            connection.commit()
        return KpiIngestResult(tuple(resolved), accepted, duplicates)

    def list_snapshots(
        self, kpi_id: str, *, account_id: str, space_id: str, limit: int = 30,
    ) -> list[KpiSnapshot]:
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM kpi_snapshots "
                "WHERE kpi_id = %s AND account_id = %s AND space_id = %s "
                "ORDER BY observed_at DESC, id DESC LIMIT %s",
                (kpi_id, account_id, space_id, bounded_history_limit(limit)),
            )
            rows = cursor.fetchall()
        return list(reversed([self._snapshot_row(row) for row in rows]))

    def dashboard(
        self,
        account_id: str,
        space_id: str,
        *,
        history_limit: int = 30,
        include_archived: bool = False,
    ) -> list[KpiSeries]:
        definitions = self.list_definitions(account_id, space_id, include_archived=include_archived)
        if not definitions:
            return []
        ids = [row.id for row in definitions]
        limit = bounded_history_limit(history_limit)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_SNAPSHOT_COLUMNS}
                FROM (
                    SELECT {_SNAPSHOT_COLUMNS},
                           row_number() OVER (PARTITION BY kpi_id ORDER BY observed_at DESC, id DESC) AS position
                    FROM kpi_snapshots
                    WHERE account_id = %s AND space_id = %s AND kpi_id = ANY(%s)
                ) ranked
                WHERE position <= %s
                ORDER BY kpi_id, observed_at, id
                """,
                (account_id, space_id, ids, limit),
            )
            snapshot_rows = cursor.fetchall()
        by_kpi: dict[str, list[KpiSnapshot]] = {kpi_id: [] for kpi_id in ids}
        for row in snapshot_rows:
            snapshot = self._snapshot_row(row)
            by_kpi[snapshot.kpi_id].append(snapshot)
        return [KpiSeries(definition, tuple(by_kpi[definition.id])) for definition in definitions]

    def export_scope(self, account_id: str, space_id: str = "") -> dict:
        clause = "account_id = %s"
        params: tuple = (account_id,)
        if space_id:
            clause += " AND space_id = %s"
            params = (account_id, space_id)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_DEFINITION_COLUMNS} FROM kpi_definitions WHERE {clause} ORDER BY space_id, key",
                params,
            )
            definitions = [self._definition_row(row) for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM kpi_snapshots WHERE {clause} ORDER BY observed_at, id",
                params,
            )
            snapshots = [self._snapshot_row(row) for row in cursor.fetchall()]
        return {
            "definitions": [definition_to_dict(row) for row in definitions],
            "snapshots": [snapshot_to_dict(row) for row in snapshots],
        }

    def delete_scope(self, account_id: str, space_id: str = "") -> dict[str, int]:
        clause = "account_id = %s"
        params: tuple = (account_id,)
        if space_id:
            clause += " AND space_id = %s"
            params = (account_id, space_id)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM kpi_snapshots WHERE {clause}", params)
            snapshot_count = cursor.fetchone()[0]
            cursor.execute(f"DELETE FROM kpi_definitions WHERE {clause}", params)
            definition_count = cursor.rowcount
            connection.commit()
        return {"definitions": definition_count, "snapshots": snapshot_count}

    def retention_scope(
        self,
        account_id: str,
        space_id: str,
        *,
        older_than: str,
        delete: bool,
    ) -> dict[str, int]:
        clause = "account_id = %s AND received_at < %s"
        params: list = [account_id, older_than]
        if space_id:
            clause += " AND space_id = %s"
            params.append(space_id)
        with self._conn(account_id=account_id, space_id=space_id) as connection, connection.cursor() as cursor:
            if delete:
                cursor.execute(f"DELETE FROM kpi_snapshots WHERE {clause}", tuple(params))
                count = cursor.rowcount
                connection.commit()
            else:
                cursor.execute(f"SELECT count(*) FROM kpi_snapshots WHERE {clause}", tuple(params))
                count = cursor.fetchone()[0]
        return {"snapshots": count, "snapshots_deleted": count if delete else 0}

    @staticmethod
    def _definition_values(definition: KpiDefinition) -> tuple:
        return (
            definition.id, definition.account_id, definition.space_id, definition.key,
            definition.name, definition.description, definition.category, definition.unit,
            definition.source_label, definition.owner_label, definition.freshness_minutes,
            definition.warning_min, definition.warning_max, definition.critical_min,
            definition.critical_max, definition.display_order, definition.status,
        )

    @staticmethod
    def _lock_scope(cursor, account_id: str, space_id: str) -> None:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"kpi:{account_id}:{space_id}",))

    @staticmethod
    def _check_definition_limits(
        cursor,
        account_id: str,
        space_id: str,
        status: str,
        *,
        excluding_id: str = "",
    ) -> None:
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE status = 'active') "
            "FROM kpi_definitions WHERE account_id = %s AND space_id = %s AND id <> %s",
            (account_id, space_id, excluding_id),
        )
        total, active = cursor.fetchone()
        if total >= MAX_DEFINITIONS_PER_SPACE:
            raise KpiLimitError(f"A space may hold at most {MAX_DEFINITIONS_PER_SPACE} KPI definitions.")
        if status == "active" and active >= MAX_ACTIVE_DEFINITIONS_PER_SPACE:
            raise KpiLimitError(
                f"A space may hold at most {MAX_ACTIVE_DEFINITIONS_PER_SPACE} active KPI definitions.",
            )

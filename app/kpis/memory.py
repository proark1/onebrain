"""Thread-safe JSON-backed KPI store for local development and tests."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from decimal import Decimal
from typing import Optional, Sequence

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
    now_iso,
    snapshot_semantically_equal,
    validate_definition,
    validate_snapshot,
)


_THRESHOLD_FIELDS = ("warning_min", "warning_max", "critical_min", "critical_max")


def definition_to_dict(definition: KpiDefinition) -> dict:
    out = dict(definition.__dict__)
    for field in _THRESHOLD_FIELDS:
        value = out[field]
        out[field] = str(value) if value is not None else None
    return out


def definition_from_dict(data: dict) -> KpiDefinition:
    values = dict(data)
    for field in _THRESHOLD_FIELDS:
        value = values.get(field)
        values[field] = Decimal(str(value)) if value not in (None, "") else None
    return KpiDefinition(**values)


def snapshot_to_dict(snapshot: KpiSnapshot) -> dict:
    return {**snapshot.__dict__, "value": str(snapshot.value)}


def snapshot_from_dict(data: dict) -> KpiSnapshot:
    return KpiSnapshot(**{**data, "value": Decimal(str(data["value"]))})


class MemoryKpiStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._definitions: dict[str, KpiDefinition] = {}
        self._snapshots: dict[str, KpiSnapshot] = {}
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            definitions = {
                row["id"]: definition_from_dict(row)
                for row in data.get("definitions", [])
            }
            snapshots = {
                row["id"]: snapshot_from_dict(row)
                for row in data.get("snapshots", [])
            }
            for definition in definitions.values():
                validate_definition(definition)
            for snapshot in snapshots.values():
                validate_snapshot(snapshot)
            self._definitions = definitions
            self._snapshots = snapshots
        except Exception:
            # KPI persistence is its own failure domain. Never touch platform data.
            self._definitions = {}
            self._snapshots = {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "definitions": [definition_to_dict(row) for row in self._definitions.values()],
                    "snapshots": [snapshot_to_dict(row) for row in self._snapshots.values()],
                },
                handle,
            )

    def create_definition(self, definition: KpiDefinition) -> KpiDefinition:
        validate_definition(definition)
        with self._lock:
            if definition.id in self._definitions:
                raise KpiConflictError(f"KPI definition already exists: {definition.id}")
            if self.get_definition_by_key(
                definition.key, account_id=definition.account_id, space_id=definition.space_id,
            ):
                raise KpiConflictError(f"KPI key already exists in this space: {definition.key}")
            self._check_definition_limits(definition.account_id, definition.space_id, adding=definition)
            timestamp = now_iso()
            stored = replace(
                definition,
                created_at=definition.created_at or timestamp,
                updated_at=definition.updated_at or timestamp,
            )
            self._definitions[stored.id] = stored
            self._save()
            return stored

    def update_definition(self, definition: KpiDefinition) -> KpiDefinition:
        validate_definition(definition)
        with self._lock:
            current = self._definitions.get(definition.id)
            if not current or current.account_id != definition.account_id or current.space_id != definition.space_id:
                raise KeyError(f"Unknown KPI definition: {definition.id}")
            key_match = self.get_definition_by_key(
                definition.key, account_id=definition.account_id, space_id=definition.space_id,
            )
            if key_match and key_match.id != definition.id:
                raise KpiConflictError(f"KPI key already exists in this space: {definition.key}")
            if current.status != "active" and definition.status == "active":
                self._check_definition_limits(
                    definition.account_id, definition.space_id, adding=definition, excluding_id=definition.id,
                )
            stored = replace(definition, created_at=current.created_at, updated_at=now_iso())
            self._definitions[stored.id] = stored
            self._save()
            return stored

    def get_definition(
        self, kpi_id: str, *, account_id: str, space_id: str,
    ) -> Optional[KpiDefinition]:
        definition = self._definitions.get(kpi_id)
        if not definition or definition.account_id != account_id or definition.space_id != space_id:
            return None
        return definition

    def get_definition_by_key(
        self, key: str, *, account_id: str, space_id: str,
    ) -> Optional[KpiDefinition]:
        return next(
            (
                row for row in self._definitions.values()
                if row.account_id == account_id and row.space_id == space_id and row.key == key
            ),
            None,
        )

    def list_definitions(
        self, account_id: str, space_id: str, *, include_archived: bool = False,
    ) -> list[KpiDefinition]:
        rows = [
            row for row in self._definitions.values()
            if row.account_id == account_id
            and row.space_id == space_id
            and (include_archived or row.status == "active")
        ]
        return sorted(rows, key=lambda row: (row.display_order, row.name.lower(), row.id))

    def ingest_snapshots(self, snapshots: Sequence[KpiSnapshot]) -> KpiIngestResult:
        rows = list(snapshots)
        if not 1 <= len(rows) <= MAX_BATCH_SIZE:
            raise KpiLimitError(f"A snapshot batch must contain 1 to {MAX_BATCH_SIZE} items.")
        for row in rows:
            validate_snapshot(row)

        with self._lock:
            pending: list[KpiSnapshot] = []
            resolved: list[KpiSnapshot] = []
            duplicate_count = 0
            idempotency_index = {
                (row.account_id, row.idempotency_key): row
                for row in self._snapshots.values()
            }
            observation_index = {
                (row.kpi_id, row.observed_at): row
                for row in self._snapshots.values()
            }

            for row in rows:
                definition = self.get_definition(
                    row.kpi_id, account_id=row.account_id, space_id=row.space_id,
                )
                if not definition:
                    raise ValueError("KPI definition is not in the authorized account and space.")
                by_idempotency = idempotency_index.get((row.account_id, row.idempotency_key))
                by_observation = observation_index.get((row.kpi_id, row.observed_at))
                existing = by_idempotency or by_observation
                if existing:
                    if not snapshot_semantically_equal(existing, row):
                        raise KpiConflictError("Idempotency key or KPI observation conflicts with stored data.")
                    resolved.append(existing)
                    duplicate_count += 1
                    continue
                if row.id in self._snapshots or any(item.id == row.id for item in pending):
                    raise KpiConflictError(f"Snapshot id already exists: {row.id}")
                pending.append(row)
                resolved.append(row)
                idempotency_index[(row.account_id, row.idempotency_key)] = row
                observation_index[(row.kpi_id, row.observed_at)] = row

            for row in pending:
                self._snapshots[row.id] = row
            self._save()
            return KpiIngestResult(tuple(resolved), len(pending), duplicate_count)

    def list_snapshots(
        self, kpi_id: str, *, account_id: str, space_id: str, limit: int = 30,
    ) -> list[KpiSnapshot]:
        if not self.get_definition(kpi_id, account_id=account_id, space_id=space_id):
            return []
        rows = [
            row for row in self._snapshots.values()
            if row.kpi_id == kpi_id and row.account_id == account_id and row.space_id == space_id
        ]
        rows.sort(key=lambda row: (row.observed_at, row.id), reverse=True)
        return list(reversed(rows[:bounded_history_limit(limit)]))

    def dashboard(
        self,
        account_id: str,
        space_id: str,
        *,
        history_limit: int = 30,
        include_archived: bool = False,
    ) -> list[KpiSeries]:
        return [
            KpiSeries(
                definition,
                tuple(self.list_snapshots(
                    definition.id,
                    account_id=account_id,
                    space_id=space_id,
                    limit=history_limit,
                )),
            )
            for definition in self.list_definitions(
                account_id, space_id, include_archived=include_archived,
            )
        ]

    def export_scope(self, account_id: str, space_id: str = "") -> dict:
        definitions = [
            row for row in self._definitions.values()
            if row.account_id == account_id and (not space_id or row.space_id == space_id)
        ]
        snapshots = [
            row for row in self._snapshots.values()
            if row.account_id == account_id and (not space_id or row.space_id == space_id)
        ]
        return {
            "definitions": [definition_to_dict(row) for row in definitions],
            "snapshots": [snapshot_to_dict(row) for row in snapshots],
        }

    def delete_scope(self, account_id: str, space_id: str = "") -> dict[str, int]:
        with self._lock:
            snapshot_ids = [
                row.id for row in self._snapshots.values()
                if row.account_id == account_id and (not space_id or row.space_id == space_id)
            ]
            definition_ids = [
                row.id for row in self._definitions.values()
                if row.account_id == account_id and (not space_id or row.space_id == space_id)
            ]
            for snapshot_id in snapshot_ids:
                del self._snapshots[snapshot_id]
            for definition_id in definition_ids:
                del self._definitions[definition_id]
            self._save()
            return {"definitions": len(definition_ids), "snapshots": len(snapshot_ids)}

    def retention_scope(
        self,
        account_id: str,
        space_id: str,
        *,
        older_than: str,
        delete: bool,
    ) -> dict[str, int]:
        with self._lock:
            eligible = [
                row.id for row in self._snapshots.values()
                if row.account_id == account_id
                and (not space_id or row.space_id == space_id)
                and row.received_at < older_than
            ]
            if delete:
                for snapshot_id in eligible:
                    del self._snapshots[snapshot_id]
                self._save()
            return {
                "snapshots": len(eligible),
                "snapshots_deleted": len(eligible) if delete else 0,
            }

    def _check_definition_limits(
        self,
        account_id: str,
        space_id: str,
        *,
        adding: KpiDefinition,
        excluding_id: str = "",
    ) -> None:
        rows = [
            row for row in self._definitions.values()
            if row.account_id == account_id and row.space_id == space_id and row.id != excluding_id
        ]
        if len(rows) >= MAX_DEFINITIONS_PER_SPACE:
            raise KpiLimitError(f"A space may hold at most {MAX_DEFINITIONS_PER_SPACE} KPI definitions.")
        if adding.status == "active" and sum(row.status == "active" for row in rows) >= MAX_ACTIVE_DEFINITIONS_PER_SPACE:
            raise KpiLimitError(
                f"A space may hold at most {MAX_ACTIVE_DEFINITIONS_PER_SPACE} active KPI definitions.",
            )

"""JSON-backed in-process intake store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from typing import Dict, List, Optional

from app.intake.base import IntakeRecord


def _matches_scope(record: IntakeRecord, tenant_id: str, account_id: str = "", space_id: str = "") -> bool:
    if record.tenant_id != tenant_id:
        return False
    if space_id:
        return record.account_id == account_id and record.space_id == space_id
    if account_id and record.account_id != account_id:
        return False
    return True


class MemoryIntakeStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._records: Dict[str, IntakeRecord] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                self._records = {
                    item["id"]: IntakeRecord(**item)
                    for item in json.load(fh).get("records", [])
                }
        except Exception:
            self._records = {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({"records": [asdict(record) for record in self._records.values()]}, fh)

    def create(self, record: IntakeRecord) -> IntakeRecord:
        with self._lock:
            if record.id in self._records:
                raise ValueError(f"intake record already exists: {record.id}")
            self._records[record.id] = record
            self._save()
            return record

    def get(
        self,
        record_id: str,
        tenant_id: str = "",
        account_id: str = "",
        space_id: str = "",
    ) -> Optional[IntakeRecord]:
        with self._lock:
            record = self._records.get(record_id)
            if not record or not tenant_id:
                return record
            return record if _matches_scope(record, tenant_id, account_id, space_id) else None

    def list_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[IntakeRecord]:
        with self._lock:
            records = [
                record for record in self._records.values()
                if _matches_scope(record, tenant_id, account_id, space_id)
            ]
        return sorted(records, key=lambda record: record.created_at or record.id)

    def export_records(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[dict]:
        return [asdict(record) for record in self.list_by_scope(tenant_id, account_id, space_id)]

    def delete_records_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> int:
        with self._lock:
            remove_ids = [
                record.id for record in self._records.values()
                if _matches_scope(record, tenant_id, account_id, space_id)
            ]
            for record_id in remove_ids:
                self._records.pop(record_id, None)
            if remove_ids:
                self._save()
        return len(remove_ids)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

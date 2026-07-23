"""Thread-safe JSON-backed accounting store for local development and tests.

Phase 0 skeleton: no ingest path exists yet, so the store starts empty and stays
empty until Phase 1 adds extraction. It still honours the GDPR export/erase scope
contract so the module is compliant the moment documents can be created.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

from app.accounting.base import AccountingOverview


class MemoryAccountingStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._documents: dict[str, dict] = {}
        self._line_items: dict[str, dict] = {}
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._documents = {row["id"]: row for row in data.get("documents", [])}
            self._line_items = {row["id"]: row for row in data.get("line_items", [])}
        except Exception:
            # Accounting persistence is its own failure domain. Never touch platform data.
            self._documents = {}
            self._line_items = {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "documents": list(self._documents.values()),
                    "line_items": list(self._line_items.values()),
                },
                handle,
            )

    def overview(self, account_id: str, space_id: str) -> AccountingOverview:
        documents = [
            row for row in self._documents.values()
            if row.get("account_id") == account_id and row.get("space_id") == space_id
        ]
        pending = sum(1 for row in documents if row.get("status") == "pending")
        confirmed = sum(1 for row in documents if row.get("status") == "confirmed")
        return AccountingOverview(
            account_id=account_id,
            space_id=space_id,
            total_documents=len(documents),
            pending_documents=pending,
            confirmed_documents=confirmed,
        )

    def export_scope(self, account_id: str, space_id: str = "") -> dict:
        documents = [
            row for row in self._documents.values()
            if row.get("account_id") == account_id and (not space_id or row.get("space_id") == space_id)
        ]
        line_items = [
            row for row in self._line_items.values()
            if row.get("account_id") == account_id and (not space_id or row.get("space_id") == space_id)
        ]
        return {"documents": documents, "line_items": line_items}

    def delete_scope(self, account_id: str, space_id: str = "") -> dict[str, int]:
        with self._lock:
            document_ids = [
                row_id for row_id, row in self._documents.items()
                if row.get("account_id") == account_id and (not space_id or row.get("space_id") == space_id)
            ]
            line_item_ids = [
                row_id for row_id, row in self._line_items.items()
                if row.get("account_id") == account_id and (not space_id or row.get("space_id") == space_id)
            ]
            for row_id in document_ids:
                del self._documents[row_id]
            for row_id in line_item_ids:
                del self._line_items[row_id]
            self._save()
            return {"documents": len(document_ids), "line_items": len(line_item_ids)}

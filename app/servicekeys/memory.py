"""In-process service-key store with optional JSON persistence.

JSON (not pickle) is used deliberately: the file is loaded on every process
start, and unpickling an attacker-writable file would be remote code execution.
ServiceKey is a flat, trivially-serialisable record.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

from app.servicekeys.base import ServiceKey


def _to_dict(k: ServiceKey) -> dict:
    return {"id": k.id, "key_hash": k.key_hash, "tenant_id": k.tenant_id,
            "scopes": list(k.scopes), "label": k.label, "account_id": k.account_id, "app_id": k.app_id,
            "space_ids": list(k.space_ids), "purposes": list(k.purposes),
            "status": k.status, "created_at": k.created_at}


def _from_dict(d: dict) -> ServiceKey:
    return ServiceKey(id=d["id"], key_hash=d["key_hash"], tenant_id=d["tenant_id"],
                      scopes=tuple(d.get("scopes", [])), label=d.get("label", ""),
                      account_id=d.get("account_id", ""), app_id=d.get("app_id", ""),
                      space_ids=tuple(d.get("space_ids", [])), purposes=tuple(d.get("purposes", [])),
                      status=d.get("status", "active"), created_at=d.get("created_at", ""))


class MemoryServiceKeyStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._by_id: Dict[str, ServiceKey] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                self._by_id = {d["id"]: _from_dict(d) for d in json.load(fh)}
        except Exception:
            # A corrupt or legacy (pickle) file must not brick startup or be
            # unpickled — start empty; keys can be re-minted.
            self._by_id = {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump([_to_dict(k) for k in self._by_id.values()], fh)

    def get(self, key_id: str) -> Optional[ServiceKey]:
        return self._by_id.get(key_id)

    def create(self, key: ServiceKey) -> ServiceKey:
        with self._lock:
            if key.id in self._by_id:
                raise ValueError(f"service key id already exists: {key.id}")
            self._by_id[key.id] = key
            self._save()
            return key

    def list_by_tenant(self, tenant_id: str) -> List[ServiceKey]:
        return [k for k in self._by_id.values() if k.tenant_id == tenant_id]

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            key = self._by_id.get(key_id)
            if not key:
                return False
            key.status = "revoked"
            self._save()
            return True

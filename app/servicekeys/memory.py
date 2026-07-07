"""In-process service-key store with optional pickle persistence."""

from __future__ import annotations

import os
import pickle
import threading
from typing import Dict, List, Optional

from app.servicekeys.base import ServiceKey


class MemoryServiceKeyStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._by_id: Dict[str, ServiceKey] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if self._persist_path and os.path.exists(self._persist_path):
            with open(self._persist_path, "rb") as fh:
                self._by_id = pickle.load(fh)

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "wb") as fh:
            pickle.dump(self._by_id, fh)

    def get(self, key_id: str) -> Optional[ServiceKey]:
        return self._by_id.get(key_id)

    def create(self, key: ServiceKey) -> ServiceKey:
        with self._lock:
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

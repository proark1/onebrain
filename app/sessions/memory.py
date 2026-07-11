"""In-process session store with optional pickle persistence (local/dev/test)."""

from __future__ import annotations

import os
import pickle
import threading
from typing import Dict, List, Optional

from app.sessions.base import Session


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class MemorySessionStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._by_id: Dict[str, Session] = {}
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

    def create(self, session: Session) -> Session:
        with self._lock:
            self._by_id[session.id] = session
            self._save()
            return session

    def get(self, session_id: str) -> Optional[Session]:
        return self._by_id.get(session_id)

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            session = self._by_id.get(session_id)
            if not session or session.revoked_at:
                return False
            session.revoked_at = _now_iso()
            self._save()
            return True

    def revoke_all_for_user(self, user_id: str) -> int:
        with self._lock:
            now = _now_iso()
            count = 0
            for session in self._by_id.values():
                if session.user_id == user_id and not session.revoked_at:
                    session.revoked_at = now
                    count += 1
            if count:
                self._save()
            return count

    def list_for_user(self, user_id: str) -> List[Session]:
        return sorted(
            (s for s in self._by_id.values() if s.user_id == user_id),
            key=lambda s: s.created_at,
        )

    def purge_expired(self, now_iso: str) -> int:
        with self._lock:
            stale = [sid for sid, s in self._by_id.items() if s.expires_at and s.expires_at < now_iso]
            for sid in stale:
                del self._by_id[sid]
            if stale:
                self._save()
            return len(stale)

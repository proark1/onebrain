"""In-process user store with optional pickle persistence."""

from __future__ import annotations

import os
import pickle
import threading
from dataclasses import replace
from typing import Dict, List, Optional

from app.users.base import User


class MemoryUserStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._by_id: Dict[str, User] = {}
        self._by_email: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if self._persist_path and os.path.exists(self._persist_path):
            with open(self._persist_path, "rb") as fh:
                self._by_id, self._by_email = pickle.load(fh)

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "wb") as fh:
            pickle.dump((self._by_id, self._by_email), fh)

    def get(self, user_id: str) -> Optional[User]:
        return self._by_id.get(user_id)

    def get_by_email(self, email: str) -> Optional[User]:
        uid = self._by_email.get(email.strip().lower())
        return self._by_id.get(uid) if uid else None

    def create(self, user: User) -> User:
        with self._lock:
            self._by_id[user.id] = user
            self._by_email[user.email.strip().lower()] = user.id
            self._save()
            return user

    def update_password(self, user_id: str, password_hash: str, *, must_change_password: bool) -> User:
        with self._lock:
            user = self._by_id.get(user_id)
            if not user:
                raise KeyError(f"unknown user: {user_id}")
            updated = replace(user, password_hash=password_hash, must_change_password=must_change_password)
            self._by_id[user_id] = updated
            self._save()
            return updated

    def update_scope(self, user_id: str, *, tenant_id: str, role_id: str, location: str) -> User:
        with self._lock:
            user = self._by_id.get(user_id)
            if not user:
                raise KeyError(f"unknown user: {user_id}")
            updated = replace(user, tenant_id=tenant_id, role_id=role_id, location=location)
            self._by_id[user_id] = updated
            self._save()
            return updated

    def update_status(self, user_id: str, status: str) -> User:
        with self._lock:
            user = self._by_id.get(user_id)
            if not user:
                raise KeyError(f"unknown user: {user_id}")
            updated = replace(user, status=status)
            self._by_id[user_id] = updated
            self._save()
            return updated

    def anonymize(self, user_id: str, *, email: str, password_hash: str) -> User:
        with self._lock:
            user = self._by_id.get(user_id)
            if not user:
                raise KeyError(f"unknown user: {user_id}")
            self._by_email.pop(user.email.strip().lower(), None)
            updated = replace(
                user,
                email=email.strip().lower(),
                display_name="Deleted user",
                password_hash=password_hash,
                role_id="public",
                location="",
                status="deleted",
                must_change_password=False,
            )
            self._by_id[user_id] = updated
            self._by_email[updated.email] = user_id
            self._save()
            return updated

    def delete_by_email(self, email: str) -> bool:
        with self._lock:
            key = email.strip().lower()
            uid = self._by_email.pop(key, None)
            if uid is None:
                return False
            self._by_id.pop(uid, None)
            self._save()
            return True

    def count(self) -> int:
        return len(self._by_id)

    def list_by_tenant(self, tenant_id: str) -> List[User]:
        return [u for u in self._by_id.values() if u.tenant_id == tenant_id]

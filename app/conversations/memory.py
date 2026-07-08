"""In-process conversation store with optional pickle persistence."""

from __future__ import annotations

import os
import pickle
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.conversations.base import Conversation, Message, Scope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owns(conv: Conversation, scope: Scope) -> bool:
    return (
        conv.tenant_id, conv.session_id, conv.role_id,
        getattr(conv, "account_id", ""), getattr(conv, "space_id", ""),
    ) == (
        scope.tenant_id, scope.session_id, scope.role_id, scope.account_id, scope.space_id,
    )


class MemoryConversationStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._conversations: Dict[str, Conversation] = {}
        self._messages: Dict[str, List[Message]] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if self._persist_path and os.path.exists(self._persist_path):
            with open(self._persist_path, "rb") as fh:
                self._conversations, self._messages = pickle.load(fh)

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "wb") as fh:
            pickle.dump((self._conversations, self._messages), fh)

    def create(self, scope: Scope, title: str) -> Conversation:
        with self._lock:
            conv = Conversation(
                id=uuid.uuid4().hex, tenant_id=scope.tenant_id, session_id=scope.session_id,
                role_id=scope.role_id, title=title[:80] or "New chat", created_at=_now(), updated_at=_now(),
                account_id=scope.account_id, space_id=scope.space_id,
            )
            self._conversations[conv.id] = conv
            self._messages[conv.id] = []
            self._save()
            return conv

    def get(self, conversation_id: str, scope: Scope) -> Optional[Conversation]:
        conv = self._conversations.get(conversation_id)
        return conv if conv and _owns(conv, scope) else None

    def list(self, scope: Scope, limit: int = 50) -> List[Conversation]:
        with self._lock:
            owned = [c for c in self._conversations.values() if _owns(c, scope)]
        owned.sort(key=lambda c: c.updated_at, reverse=True)
        return owned[:limit]

    def add_message(self, conversation_id: str, role: str, content: str, meta: Optional[dict] = None) -> Message:
        with self._lock:
            msg = Message(role=role, content=content, id=uuid.uuid4().hex, created_at=_now(), meta=meta or {})
            self._messages.setdefault(conversation_id, []).append(msg)
            if conversation_id in self._conversations:
                self._conversations[conversation_id].updated_at = _now()
            self._save()
            return msg

    def get_messages(self, conversation_id: str, limit: Optional[int] = None) -> List[Message]:
        msgs = list(self._messages.get(conversation_id, []))
        return msgs[-limit:] if limit else msgs

    def delete(self, conversation_id: str, scope: Scope) -> bool:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if not conv or not _owns(conv, scope):
                return False
            self._conversations.pop(conversation_id, None)
            self._messages.pop(conversation_id, None)
            self._save()
            return True

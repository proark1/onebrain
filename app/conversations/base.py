"""Conversation store interface and types.

A conversation is scoped by (tenant_id, session_id, role_id): a device's chats,
for one tenant, at one clearance. Switching roles gives a separate thread — so a
lower-tier session never inherits history that a higher tier produced. This is
also a PII sink: it stores user messages, so it's retention-bounded and erasable
(delete()).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass
class Message:
    role: str          # "user" | "assistant"
    content: str
    id: str = ""
    created_at: str = ""
    meta: dict = field(default_factory=dict)   # assistant: tokens/cost/sources


@dataclass
class Conversation:
    id: str
    tenant_id: str
    session_id: str
    role_id: str
    title: str
    created_at: str = ""
    updated_at: str = ""
    account_id: str = ""
    space_id: str = ""


@dataclass(frozen=True)
class Scope:
    """The (tenant, session, role) triple that owns a conversation."""
    tenant_id: str
    session_id: str
    role_id: str
    account_id: str = ""
    space_id: str = ""


class ConversationStore(Protocol):
    def create(self, scope: Scope, title: str) -> Conversation: ...

    def get(self, conversation_id: str, scope: Scope) -> Optional[Conversation]: ...

    def list(self, scope: Scope, limit: int = 50) -> List[Conversation]: ...

    def add_message(self, conversation_id: str, role: str, content: str, meta: Optional[dict] = None) -> Message: ...

    def get_messages(self, conversation_id: str, limit: Optional[int] = None) -> List[Message]: ...

    def delete(self, conversation_id: str, scope: Scope) -> bool: ...

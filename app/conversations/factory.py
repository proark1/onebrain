"""Pick the conversation store — Postgres when the app runs on pgvector, else memory."""

from __future__ import annotations

import os

from app.config import Settings


def build_conversation_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.conversations.postgres import PostgresConversationStore

        return PostgresConversationStore(settings.database_url)

    from app.conversations.memory import MemoryConversationStore

    path = os.path.join(settings.data_dir, "conversations.pkl") if settings.persist else None
    return MemoryConversationStore(persist_path=path)

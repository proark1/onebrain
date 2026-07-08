"""Human chat can be scoped to a OneBrain platform space."""

from __future__ import annotations

import json

import anyio
import pytest
from fastapi import HTTPException

import app.routers.chat as chat_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.conversations.base import Scope
from app.conversations.memory import MemoryConversationStore
from app.platform.base import Account, Space
from app.platform.memory import MemoryPlatformStore
from app.schemas import AskRequest


class FakeRetrieval:
    def __init__(self):
        self.principals = []

    def answer_stream(self, principal, question, history=None):
        self.principals.append(principal)
        yield {"type": "token", "text": "scoped answer"}
        yield {"type": "sources", "sources": []}
        yield {"type": "meta", "chunks_used": 0, "total_tokens": 1, "cost_usd": 0, "estimated": True, "llm": "fake"}
        yield {"type": "done"}


def _principal(role_id: str = "admin") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@nft_gym",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"munich"}),
        categories=role.categories,
        location_label="munich",
        tenant_id="nft_gym",
    )


def _platform_store() -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="nft_gym", kind="organization", name="NFT Gym"))
    platform.create_space(Space(id="sp_customer", account_id="nft_gym", kind="customer_service", name="Customer service"))
    platform.create_space(Space(id="sp_personal", account_id="nft_gym", kind="personal", name="Personal"))
    return platform


def _event_payloads(response):
    payloads = []

    async def collect():
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            for part in text.strip().split("\n\n"):
                if part.startswith("data:"):
                    payloads.append(json.loads(part[5:].strip()))

    anyio.run(collect)
    return payloads


def test_chat_uses_scoped_principal_and_space_isolated_conversation_store(monkeypatch):
    retrieval = FakeRetrieval()
    conversations = MemoryConversationStore()
    monkeypatch.setattr(chat_router, "get_retrieval_service", lambda: retrieval)
    monkeypatch.setattr(chat_router, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(chat_router, "get_platform_store", lambda: _platform_store())

    response = chat_router.ask(
        AskRequest(question="What support notes exist?", account_id="nft_gym", space_id="sp_customer"),
        principal=_principal("admin"),
    )
    payloads = _event_payloads(response)

    assert payloads[0]["type"] == "conversation"
    assert retrieval.principals[0].account_id == "nft_gym"
    assert retrieval.principals[0].space_ids == frozenset({"sp_customer"})
    assert conversations.list(Scope("nft_gym", "admin@nft_gym", "admin")) == []
    assert len(conversations.list(Scope("nft_gym", "admin@nft_gym", "admin", "nft_gym", "sp_customer"))) == 1


def test_chat_space_scope_requires_admin(monkeypatch):
    monkeypatch.setattr(chat_router, "get_platform_store", lambda: _platform_store())

    with pytest.raises(HTTPException) as exc:
        chat_router.ask(
            AskRequest(question="Can I see the customer space?", account_id="nft_gym", space_id="sp_customer"),
            principal=_principal("front_desk"),
        )

    assert exc.value.status_code == 403

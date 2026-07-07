"""Conversation persistence, scope isolation, and history-in-prompt."""

from __future__ import annotations

from app.conversations.base import Scope
from app.conversations.memory import MemoryConversationStore
from app.llm.prompt import build_messages

SCOPE_A = Scope("nft_gym", "sess-A", "admin")
SCOPE_B = Scope("nft_gym", "sess-B", "admin")          # different device
SCOPE_C = Scope("nft_gym", "sess-A", "front_desk")     # same device, different role
SCOPE_D = Scope("companyB", "sess-A", "admin")         # different tenant


def test_create_and_load_roundtrip():
    store = MemoryConversationStore()
    conv = store.create(SCOPE_A, "Opening hours question")
    store.add_message(conv.id, "user", "What are the opening hours?")
    store.add_message(conv.id, "assistant", "Munich is open 06:00-23:00.")
    msgs = store.get_messages(conv.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert store.get(conv.id, SCOPE_A).title == "Opening hours question"


def test_conversations_are_scope_isolated():
    store = MemoryConversationStore()
    conv = store.create(SCOPE_A, "private")
    # Another device, another role, or another tenant cannot see or load it.
    for other in (SCOPE_B, SCOPE_C, SCOPE_D):
        assert store.get(conv.id, other) is None
        assert conv.id not in {c.id for c in store.list(other)}
    assert conv.id in {c.id for c in store.list(SCOPE_A)}


def test_delete_is_scoped():
    store = MemoryConversationStore()
    conv = store.create(SCOPE_A, "x")
    assert store.delete(conv.id, SCOPE_C) is False   # wrong role can't delete
    assert store.delete(conv.id, SCOPE_A) is True
    assert store.get(conv.id, SCOPE_A) is None


def test_list_orders_by_recent_and_limits():
    store = MemoryConversationStore()
    first = store.create(SCOPE_A, "first")
    second = store.create(SCOPE_A, "second")
    store.add_message(first.id, "user", "bump")  # touches updated_at
    ids = [c.id for c in store.list(SCOPE_A)]
    assert ids[0] == first.id and second.id in ids


def test_history_is_included_in_prompt():
    history = [
        {"role": "user", "content": "What marketing do they want?"},
        {"role": "assistant", "content": "They want to expand internationally."},
    ]
    messages = build_messages("And the budget for it?", hits=[], tenant_id="nft_gym", history=history)
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert "budget" in messages[-1]["content"]
    assert "international" in messages[2]["content"]

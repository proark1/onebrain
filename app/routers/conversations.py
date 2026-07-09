"""Saved chat sessions — list, load, and erase, scoped to (tenant, session, role)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.principal import Principal, resolve_principal
from app.conversations.base import Scope
from app.deps import get_conversation_store, get_platform_store
from app.platform.scope import scoped_human_principal, selected_space_id
from app.schemas import ConversationDetail, ConversationSummary, MessageOut

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def scope_of(principal: Principal) -> Scope:
    # Chats are owned by the authenticated USER (per-user history, ChatGPT-style).
    return Scope(
        tenant_id=principal.tenant_id,
        session_id=principal.user_id,
        role_id=principal.role_id,
        account_id=principal.account_id,
        space_id=selected_space_id(principal),
    )


def _scoped_scope(account_id: str, space_id: str, principal: Principal) -> Scope:
    scoped = scoped_human_principal(account_id, space_id, principal, get_platform_store())
    return scope_of(scoped)


@router.get("", response_model=list[ConversationSummary])
def list_conversations(
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    convs = get_conversation_store().list(_scoped_scope(account_id, space_id, principal))
    return [ConversationSummary(id=c.id, title=c.title, updated_at=c.updated_at) for c in convs]


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    store = get_conversation_store()
    scope = _scoped_scope(account_id, space_id, principal)
    conv = store.get(conversation_id, scope)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages = store.get_messages(conversation_id, scope=scope)
    return ConversationDetail(
        id=conv.id, title=conv.title,
        messages=[MessageOut(role=m.role, content=m.content, meta=m.meta or {}) for m in messages],
    )


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    if not get_conversation_store().delete(conversation_id, _scoped_scope(account_id, space_id, principal)):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": conversation_id}

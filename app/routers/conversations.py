"""Saved chat sessions — list, load, and erase, scoped to (tenant, session, role)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.auth.principal import Principal, resolve_principal
from app.conversations.base import Scope
from app.deps import get_conversation_store
from app.schemas import ConversationDetail, ConversationSummary, MessageOut

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def get_session_id(x_onebrain_session: str = Header(default="")) -> str:
    return (x_onebrain_session or "anon").strip() or "anon"


def scope_of(principal: Principal, session_id: str) -> Scope:
    return Scope(tenant_id=principal.tenant_id, session_id=session_id, role_id=principal.role_id)


@router.get("", response_model=list[ConversationSummary])
def list_conversations(
    principal: Principal = Depends(resolve_principal),
    session_id: str = Depends(get_session_id),
):
    convs = get_conversation_store().list(scope_of(principal, session_id))
    return [ConversationSummary(id=c.id, title=c.title, updated_at=c.updated_at) for c in convs]


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    principal: Principal = Depends(resolve_principal),
    session_id: str = Depends(get_session_id),
):
    store = get_conversation_store()
    conv = store.get(conversation_id, scope_of(principal, session_id))
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages = store.get_messages(conversation_id)
    return ConversationDetail(
        id=conv.id, title=conv.title,
        messages=[MessageOut(role=m.role, content=m.content, meta=m.meta or {}) for m in messages],
    )


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    principal: Principal = Depends(resolve_principal),
    session_id: str = Depends(get_session_id),
):
    if not get_conversation_store().delete(conversation_id, scope_of(principal, session_id)):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": conversation_id}

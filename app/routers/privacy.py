"""GDPR/privacy operations for account and space data."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.principal import Principal, resolve_principal
from app.deps import get_conversation_store, get_platform_store, get_store
from app.platform.base import AuditEvent

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


class PrivacyAuditOut(BaseModel):
    id: str
    account_id: str
    actor_id: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    space_id: str = ""
    purpose: str = ""
    decision: str = ""
    meta: dict = Field(default_factory=dict)
    created_at: str = ""


class PrivacyExportOut(BaseModel):
    account_id: str
    space_id: str = ""
    exported_at: str
    documents: list[dict]
    conversations: list[dict]
    audit_events: list[PrivacyAuditOut]


class PrivacyEraseRequest(BaseModel):
    confirm_account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(default="", max_length=120)
    reason: str = Field(default="", max_length=500)


class PrivacyEraseOut(BaseModel):
    account_id: str
    space_id: str = ""
    documents_deleted: int
    chunks_deleted: int
    conversations_deleted: int
    audit_event_id: str


def _require_admin(principal: Principal) -> None:
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin / DPO can run privacy operations.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_out(event: AuditEvent) -> PrivacyAuditOut:
    return PrivacyAuditOut(
        id=event.id,
        account_id=event.account_id,
        actor_id=event.actor_id,
        actor_type=event.actor_type,
        action=event.action,
        target_type=event.target_type,
        target_id=event.target_id,
        space_id=event.space_id,
        purpose=event.purpose,
        decision=event.decision,
        meta=event.meta,
        created_at=event.created_at,
    )


def _resolve_scope(account_id: str, space_id: str = ""):
    account_id = (account_id or "").strip()
    space_id = (space_id or "").strip()
    store = get_platform_store()
    if not store.get_account(account_id):
        raise HTTPException(status_code=404, detail="Account not found.")
    if space_id:
        space = store.get_space(space_id)
        if not space or space.account_id != account_id:
            raise HTTPException(status_code=404, detail="Space not found for this account.")
    return account_id, space_id


def _record_privacy_audit(
    principal: Principal,
    *,
    account_id: str,
    space_id: str,
    action: str,
    purpose: str,
    meta: dict,
) -> AuditEvent:
    event = AuditEvent(
        id=f"aud_privacy_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type="space" if space_id else "account",
        target_id=space_id or account_id,
        space_id=space_id,
        app_id="onebrain_core",
        purpose=purpose,
        decision="completed",
        meta=meta,
    )
    return get_platform_store().record_audit(event)


@router.get("/accounts/{account_id}/export", response_model=PrivacyExportOut)
def export_account_data(
    account_id: str,
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    account_id, space_id = _resolve_scope(account_id, space_id)
    documents = get_store().export_documents(account_id, account_id=account_id, space_id=space_id)
    conversations = get_conversation_store().export_scope(account_id, account_id=account_id, space_id=space_id)
    audit_events = [_audit_out(event) for event in get_platform_store().list_audit(account_id)]
    _record_privacy_audit(
        principal,
        account_id=account_id,
        space_id=space_id,
        action="privacy.exported",
        purpose="gdpr_export",
        meta={
            "documents": len(documents),
            "chunks": sum(len(doc.get("chunks", [])) for doc in documents),
            "conversations": len(conversations),
        },
    )
    return PrivacyExportOut(
        account_id=account_id,
        space_id=space_id,
        exported_at=_now(),
        documents=documents,
        conversations=conversations,
        audit_events=audit_events,
    )


@router.post("/accounts/{account_id}/erase", response_model=PrivacyEraseOut)
def erase_account_data(
    account_id: str,
    body: PrivacyEraseRequest,
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    account_id, space_id = _resolve_scope(account_id, body.space_id)
    if body.confirm_account_id.strip() != account_id:
        raise HTTPException(status_code=400, detail="confirm_account_id must match the account being erased.")

    deleted_docs = get_store().delete_documents_by_scope(account_id, account_id=account_id, space_id=space_id)
    deleted_conversations = get_conversation_store().delete_scope(account_id, account_id=account_id, space_id=space_id)
    audit = _record_privacy_audit(
        principal,
        account_id=account_id,
        space_id=space_id,
        action="privacy.erased",
        purpose="gdpr_delete",
        meta={
            "documents_deleted": deleted_docs["documents"],
            "chunks_deleted": deleted_docs["chunks"],
            "conversations_deleted": deleted_conversations,
            "reason": body.reason.strip(),
        },
    )
    return PrivacyEraseOut(
        account_id=account_id,
        space_id=space_id,
        documents_deleted=deleted_docs["documents"],
        chunks_deleted=deleted_docs["chunks"],
        conversations_deleted=deleted_conversations,
        audit_event_id=audit.id,
    )

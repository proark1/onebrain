"""GDPR/privacy operations for account and space data."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.account_access import authorize_account_admin
from app.auth.principal import Principal, resolve_principal
from app.deps import get_conversation_store, get_intake_store, get_kpi_store, get_platform_store, get_store
from app.platform.base import AuditEvent, LegalHold, Tombstone, scope_is_held

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
    intake_records: list[dict] = Field(default_factory=list)
    kpis: dict = Field(default_factory=dict)
    governance: dict = Field(default_factory=dict)
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
    intake_records_deleted: int = 0
    kpis_deleted: dict = Field(default_factory=dict)
    governance_deleted: dict = Field(default_factory=dict)
    audit_event_id: str


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
    decision: str = "completed",
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
        decision=decision,
        meta=meta,
    )
    return get_platform_store().record_audit(event)


@router.get("/accounts/{account_id}/export", response_model=PrivacyExportOut)
def export_account_data(
    account_id: str,
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    account_id, space_id = _resolve_scope(account_id, space_id)
    documents = get_store().export_documents(account_id, account_id=account_id, space_id=space_id)
    conversations = get_conversation_store().export_scope(account_id, account_id=account_id, space_id=space_id)
    intake_records = get_intake_store().export_records(account_id, account_id=account_id, space_id=space_id)
    kpis = get_kpi_store().export_scope(account_id, space_id)
    platform = get_platform_store()
    governance = {
        "organizations": [row.__dict__ for row in ([] if space_id else platform.list_organizations(account_id))],
        "memberships": [
            row.__dict__ for row in platform.list_memberships(account_id)
            if not space_id or row.space_id == space_id
        ],
        "consent_records": [row.__dict__ for row in platform.list_consent_records(account_id, space_id)],
        "retention_policies": [row.__dict__ for row in platform.list_retention_policies(account_id, space_id)],
        "data_access_events": [row.__dict__ for row in platform.list_data_access_events(account_id, space_id)],
        "processors": [row.__dict__ for row in ([] if space_id else platform.list_processors(account_id))],
        "providers": [row.__dict__ for row in ([] if space_id else platform.list_providers(account_id))],
        "credential_metadata": [row.__dict__ for row in ([] if space_id else platform.list_credential_metadata(account_id))],
    }
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
            "intake_records": len(intake_records),
            "kpis": {key: len(value) for key, value in kpis.items()},
            "governance": {key: len(value) for key, value in governance.items()},
        },
    )
    return PrivacyExportOut(
        account_id=account_id,
        space_id=space_id,
        exported_at=_now(),
        documents=documents,
        conversations=conversations,
        intake_records=intake_records,
        kpis=kpis,
        governance=governance,
        audit_events=audit_events,
    )


@router.post("/accounts/{account_id}/erase", response_model=PrivacyEraseOut)
def erase_account_data(
    account_id: str,
    body: PrivacyEraseRequest,
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    account_id, space_id = _resolve_scope(account_id, body.space_id)
    if body.confirm_account_id.strip() != account_id:
        raise HTTPException(status_code=400, detail="confirm_account_id must match the account being erased.")

    # Legal hold beats erasure. A held scope is refused with an audited denial —
    # never silently partially deleted.
    if scope_is_held(get_platform_store().list_legal_holds(account_id), space_id):
        _record_privacy_audit(
            principal,
            account_id=account_id,
            space_id=space_id,
            action="privacy.erase_denied",
            purpose="gdpr_delete",
            decision="denied_legal_hold",
            meta={"reason": body.reason.strip()},
        )
        raise HTTPException(
            status_code=409,
            detail="This scope is under an active legal hold and cannot be erased. Release the hold first.",
        )

    deleted_docs = get_store().delete_documents_by_scope(account_id, account_id=account_id, space_id=space_id)
    deleted_conversations = get_conversation_store().delete_scope(account_id, account_id=account_id, space_id=space_id)
    deleted_records = get_intake_store().delete_records_by_scope(account_id, account_id=account_id, space_id=space_id)
    deleted_kpis = get_kpi_store().delete_scope(account_id, space_id=space_id)
    deleted_governance = get_platform_store().delete_governance_by_scope(account_id, space_id=space_id)
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
            "intake_records_deleted": deleted_records,
            "kpis_deleted": deleted_kpis,
            "governance_deleted": deleted_governance,
            "reason": body.reason.strip(),
        },
    )
    # Emit a tombstone so modules holding their own copies mirror the erasure.
    get_platform_store().create_tombstone(Tombstone(
        id=f"tomb_{uuid4().hex}",
        account_id=account_id,
        space_id=space_id,
        target_type="space" if space_id else "account",
        reason=body.reason.strip(),
        created_by=principal.user_id,
        created_at=_now(),
    ))
    return PrivacyEraseOut(
        account_id=account_id,
        space_id=space_id,
        documents_deleted=deleted_docs["documents"],
        chunks_deleted=deleted_docs["chunks"],
        conversations_deleted=deleted_conversations,
        intake_records_deleted=deleted_records,
        kpis_deleted=deleted_kpis,
        governance_deleted=deleted_governance,
        audit_event_id=audit.id,
    )


class LegalHoldCreate(BaseModel):
    space_id: str = Field(default="", max_length=120)
    subject_ref: str = Field(default="", max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    legal_basis: str = Field(default="", max_length=200)


class LegalHoldOut(BaseModel):
    id: str
    account_id: str
    space_id: str = ""
    subject_ref: str = ""
    reason: str = ""
    legal_basis: str = ""
    created_by: str = ""
    created_at: str = ""
    released_at: str = ""
    active: bool = True


def _hold_out(hold: LegalHold) -> LegalHoldOut:
    return LegalHoldOut(
        id=hold.id, account_id=hold.account_id, space_id=hold.space_id,
        subject_ref=hold.subject_ref, reason=hold.reason, legal_basis=hold.legal_basis,
        created_by=hold.created_by, created_at=hold.created_at, released_at=hold.released_at,
        active=hold.active,
    )


@router.post("/accounts/{account_id}/legal-holds", response_model=LegalHoldOut)
def create_account_legal_hold(
    account_id: str,
    body: LegalHoldCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    account_id, space_id = _resolve_scope(account_id, body.space_id)
    hold = get_platform_store().create_legal_hold(LegalHold(
        id=f"hold_{uuid4().hex}",
        account_id=account_id,
        space_id=space_id,
        subject_ref=body.subject_ref.strip(),
        reason=body.reason.strip(),
        legal_basis=body.legal_basis.strip(),
        created_by=principal.user_id,
        created_at=_now(),
    ))
    _record_privacy_audit(
        principal,
        account_id=account_id,
        space_id=space_id,
        action="legal_hold.created",
        purpose="gdpr_delete",
        meta={"hold_id": hold.id, "subject_ref": hold.subject_ref, "reason": hold.reason},
    )
    return _hold_out(hold)


@router.get("/accounts/{account_id}/legal-holds", response_model=list[LegalHoldOut])
def list_account_legal_holds(
    account_id: str,
    include_released: bool = False,
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    account_id, _ = _resolve_scope(account_id)
    holds = get_platform_store().list_legal_holds(account_id, include_released=include_released)
    return [_hold_out(hold) for hold in holds]


@router.post("/accounts/{account_id}/legal-holds/{hold_id}/release", response_model=LegalHoldOut)
def release_account_legal_hold(
    account_id: str,
    hold_id: str,
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    account_id, _ = _resolve_scope(account_id)
    released = get_platform_store().release_legal_hold(account_id, hold_id, released_at=_now())
    if not released:
        raise HTTPException(status_code=404, detail="No such legal hold for this account.")
    _record_privacy_audit(
        principal,
        account_id=account_id,
        space_id=released.space_id,
        action="legal_hold.released",
        purpose="gdpr_delete",
        meta={"hold_id": hold_id},
    )
    return _hold_out(released)

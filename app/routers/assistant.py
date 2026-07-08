"""Assistant-specific OneBrain service contracts."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.assistant.contracts import (
    ASSISTANT_APP_ID,
    build_assistant_audit_meta,
    build_assistant_metadata,
    default_assistant_intent,
    validate_assistant_audit_action,
    validate_assistant_purpose,
)
from app.auth.principal import Principal, resolve_service_principal
from app.deps import get_intake_pipeline, get_intake_store, get_platform_store
from app.intake.base import IntakeRecord
from app.intake.pipeline import IntakeInput
from app.platform.base import AuditEvent
from app.routers.service import _intake_scope, _rate_limit, _require_scope
from app.schemas import (
    AssistantAuditEventCreate,
    AssistantAuditEventOut,
    AssistantRecordCreate,
    AssistantRecordOut,
    AssistantRecordResponse,
    ServiceIntakeRequest,
)
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE


router = APIRouter(prefix="/api/service/assistant", tags=["service-assistant"])


@router.post("/records", response_model=AssistantRecordResponse)
def create_assistant_record(
    body: AssistantRecordCreate,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    record_type = body.record_type.strip()
    purpose = body.purpose.strip()
    intent = (body.intent or default_assistant_intent(record_type)).strip()
    try:
        metadata = build_assistant_metadata(
            record_type,
            purpose,
            intent,
            metadata=body.metadata,
            provenance=body.provenance,
            retention=body.retention,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    scope_request = ServiceIntakeRequest(
        content=body.content,
        title=body.title,
        source=body.source,
        source_ref=body.source_ref,
        record_type=record_type,
        intent=intent,
        metadata=metadata,
        account_id=body.account_id,
        space_id=body.space_id,
        app_id=ASSISTANT_APP_ID,
        purpose=purpose,
    )
    fields = _intake_scope(scope_request, principal)
    try:
        record = get_intake_pipeline().ingest(IntakeInput(
            tenant_id=principal.tenant_id,
            account_id=fields["account_id"],
            space_id=fields["space_id"],
            app_id=ASSISTANT_APP_ID,
            purpose=fields["purpose"],
            content=body.content,
            title=body.title or "",
            source=body.source or "assistant",
            source_ref=body.source_ref,
            record_type=record_type,
            intent=intent,
            metadata=metadata,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _record_assistant_audit(
        principal,
        account_id=fields["account_id"],
        space_id=fields["space_id"],
        purpose=fields["purpose"],
        action="assistant.record.created",
        target_type="intake_record",
        target_id=record.id,
        decision="allowed",
        meta={
            "record_type": record.record_type,
            "intent": record.intent,
            "source": record.source,
            "source_ref": record.source_ref,
        },
    )
    return AssistantRecordResponse(record=_assistant_record_out(record))


@router.get("/records/{record_id}", response_model=AssistantRecordResponse)
def get_assistant_record(
    record_id: str,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_READ)
    _rate_limit(principal)
    record = get_intake_store().get(record_id)
    if not record or record.tenant_id != principal.tenant_id or record.app_id != ASSISTANT_APP_ID:
        raise HTTPException(status_code=404, detail="Assistant record not found.")
    _enforce_record_scope(record, principal)
    decision = get_platform_store().check_app_access(
        record.account_id, ASSISTANT_APP_ID, record.space_id, record.purpose,
    )
    _record_assistant_audit(
        principal,
        account_id=record.account_id,
        space_id=record.space_id,
        purpose=record.purpose,
        action="assistant.record.read",
        target_type="intake_record",
        target_id=record.id,
        decision="allowed" if decision.allowed else "denied",
        meta={"reason": decision.reason},
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"App access denied: {decision.reason}")
    return AssistantRecordResponse(record=_assistant_record_out(record))


@router.post("/audit", response_model=AssistantAuditEventOut)
def record_assistant_audit_event(
    body: AssistantAuditEventCreate,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    try:
        action = validate_assistant_audit_action(body.action)
        purpose = validate_assistant_purpose(body.purpose)
        meta = build_assistant_audit_meta(body.metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    scope_request = ServiceIntakeRequest(
        content=f"{action} {body.target_type} {body.target_id}",
        account_id=body.account_id,
        space_id=body.space_id,
        app_id=ASSISTANT_APP_ID,
        purpose=purpose,
    )
    fields = _intake_scope(scope_request, principal)
    event = _record_assistant_audit(
        principal,
        account_id=fields["account_id"],
        space_id=fields["space_id"],
        purpose=fields["purpose"],
        action=action,
        target_type=body.target_type,
        target_id=body.target_id,
        decision=body.decision or "recorded",
        meta=meta,
    )
    return _audit_out(event)


def _assistant_record_out(record: IntakeRecord) -> AssistantRecordOut:
    return AssistantRecordOut(
        id=record.id,
        tenant_id=record.tenant_id,
        account_id=record.account_id,
        space_id=record.space_id,
        app_id=record.app_id,
        purpose=record.purpose,
        source=record.source,
        source_ref=record.source_ref,
        record_type=record.record_type,
        intent=record.intent,
        classification=record.classification,
        confidence=record.confidence,
        status=record.status,
        title=record.title,
        content=record.content,
        summary=record.summary,
        extracted_facts=record.extracted_facts,
        metadata=record.metadata,
        created_at=record.created_at,
    )


def _audit_out(event: AuditEvent) -> AssistantAuditEventOut:
    return AssistantAuditEventOut(
        id=event.id,
        account_id=event.account_id,
        actor_id=event.actor_id,
        actor_type=event.actor_type,
        action=event.action,
        target_type=event.target_type,
        target_id=event.target_id,
        space_id=event.space_id,
        app_id=event.app_id,
        purpose=event.purpose,
        decision=event.decision,
        meta=event.meta,
        created_at=event.created_at,
    )


def _enforce_record_scope(record: IntakeRecord, principal: Principal) -> None:
    if principal.account_id and record.account_id != principal.account_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that account.")
    if principal.app_id and principal.app_id != ASSISTANT_APP_ID:
        raise HTTPException(status_code=403, detail="This service key cannot use that app.")
    if principal.space_ids is not None and record.space_id not in principal.space_ids:
        raise HTTPException(status_code=403, detail="This service key cannot use that space.")
    if principal.purposes is not None and record.purpose not in principal.purposes:
        raise HTTPException(status_code=403, detail="This service key cannot use that purpose.")


def _record_assistant_audit(
    principal: Principal,
    *,
    account_id: str,
    space_id: str,
    purpose: str,
    action: str,
    target_type: str,
    target_id: str,
    decision: str,
    meta: dict,
) -> AuditEvent:
    event = AuditEvent(
        id=f"aud_asst_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type=target_type,
        target_id=target_id,
        space_id=space_id,
        app_id=ASSISTANT_APP_ID,
        purpose=purpose,
        decision=decision,
        meta=dict(meta or {}),
    )
    return get_platform_store().record_audit(replace(event))

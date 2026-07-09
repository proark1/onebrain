"""Assistant-specific OneBrain service contracts."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from app.assistant.contracts import (
    ASSISTANT_APP_ID,
    ASSISTANT_INTENTS,
    ASSISTANT_RECORD_TYPES,
    build_assistant_audit_meta,
    build_assistant_metadata,
    default_assistant_intent,
    validate_assistant_audit_action,
    validate_assistant_purpose,
)
from app.auth.principal import Principal, resolve_service_principal
from app.deps import get_intake_pipeline, get_intake_store, get_platform_store
from app.intake.base import INTAKE_STATUSES, IntakeRecord
from app.intake.pipeline import IntakeInput
from app.platform.base import AuditEvent
from app.routers.service import _intake_scope, _rate_limit, _require_scope
from app.schemas import (
    AssistantAuditEventCreate,
    AssistantAuditEventOut,
    AssistantRecordCreate,
    AssistantRecordListResponse,
    AssistantRecordOut,
    AssistantRecordResponse,
    ServiceIntakeRequest,
)
from app.security.policy import Classification
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


@router.get("/records", response_model=AssistantRecordListResponse)
def list_assistant_records(
    record_type: str = Query(default="", max_length=80),
    intent: str = Query(default="", max_length=80),
    account_id: str = Query(default="", max_length=120),
    space_id: str = Query(default="", max_length=120),
    purpose: str = Query(default="", max_length=80),
    status: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_READ)
    _rate_limit(principal)
    filters = _assistant_record_filters(
        record_type=record_type,
        intent=intent,
        account_id=account_id,
        space_id=space_id,
        purpose=purpose,
        status=status,
        principal=principal,
    )
    records = []
    for record in get_intake_store().list_by_scope(
        principal.tenant_id,
        account_id=filters["account_id"],
        space_id=filters["space_id"],
    ):
        if len(records) >= limit:
            break
        if not _record_matches_filters(record, filters):
            continue
        if not _record_visible_to_principal(record, principal):
            continue
        if not _service_readable(record):
            continue
        decision = get_platform_store().check_app_access(
            record.account_id, ASSISTANT_APP_ID, record.space_id, record.purpose,
        )
        if decision.allowed:
            records.append(_assistant_record_out(record))

    _record_assistant_audit(
        principal,
        account_id=filters["account_id"] or principal.account_id or principal.tenant_id,
        space_id=filters["space_id"],
        purpose=filters["purpose"],
        action="assistant.records.list",
        target_type="intake_record",
        target_id="assistant.records",
        decision="allowed",
        meta={
            "record_type": filters["record_type"],
            "intent": filters["intent"],
            "status": filters["status"],
            "limit": limit,
            "result_count": len(records),
        },
    )
    return AssistantRecordListResponse(records=records)


@router.get("/records/{record_id}", response_model=AssistantRecordResponse)
def get_assistant_record(
    record_id: str,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_READ)
    _rate_limit(principal)
    record = get_intake_store().get(
        record_id,
        tenant_id=principal.tenant_id,
        account_id=principal.account_id or "",
        space_id=next(iter(principal.space_ids), "")
        if principal.space_ids is not None and len(principal.space_ids) == 1
        else "",
    )
    if not record or record.tenant_id != principal.tenant_id or record.app_id != ASSISTANT_APP_ID:
        raise HTTPException(status_code=404, detail="Assistant record not found.")
    _enforce_record_scope(record, principal)
    if not _service_readable(record):
        # Pending/quarantined, or above the assistant capture ceiling — treated as
        # not found so a service key cannot confirm a sensitive record exists.
        raise HTTPException(status_code=404, detail="Assistant record not found.")
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


def _assistant_record_filters(
    *,
    record_type: str,
    intent: str,
    account_id: str,
    space_id: str,
    purpose: str,
    status: str,
    principal: Principal,
) -> dict:
    requested_space_id = (space_id or "").strip()
    fields = {
        "record_type": (record_type or "").strip(),
        "intent": (intent or "").strip(),
        "account_id": (
            account_id or principal.account_id or (principal.tenant_id if requested_space_id else "")
        ).strip(),
        "space_id": requested_space_id,
        "purpose": (purpose or "").strip(),
        "status": (status or "").strip(),
    }
    if not fields["space_id"] and principal.space_ids is not None and len(principal.space_ids) == 1:
        fields["space_id"] = next(iter(principal.space_ids))
    if principal.app_id and principal.app_id != ASSISTANT_APP_ID:
        raise HTTPException(status_code=403, detail="This service key cannot use the assistant app.")
    if fields["account_id"] and fields["account_id"] != principal.tenant_id:
        raise HTTPException(status_code=403, detail="This service key is not pinned to that account.")
    if principal.account_id and fields["account_id"] and fields["account_id"] != principal.account_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that account.")
    if principal.space_ids is not None and fields["space_id"] and fields["space_id"] not in principal.space_ids:
        raise HTTPException(status_code=403, detail="This service key cannot use that space.")
    if principal.purposes is not None and fields["purpose"] and fields["purpose"] not in principal.purposes:
        raise HTTPException(status_code=403, detail="This service key cannot use that purpose.")
    if fields["record_type"] and fields["record_type"] not in ASSISTANT_RECORD_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown assistant record_type: {fields['record_type']}")
    if fields["intent"] and fields["intent"] not in ASSISTANT_INTENTS:
        raise HTTPException(status_code=422, detail=f"Unknown assistant intent: {fields['intent']}")
    if fields["status"] and fields["status"] not in INTAKE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown intake status: {fields['status']}")
    return fields


def _record_matches_filters(record: IntakeRecord, filters: dict) -> bool:
    if filters.get("account_id") and record.account_id != filters["account_id"]:
        return False
    if record.app_id != ASSISTANT_APP_ID:
        return False
    for key in ("record_type", "intent", "purpose", "status"):
        value = filters.get(key)
        if value and getattr(record, key) != value:
            return False
    return True


def _record_visible_to_principal(record: IntakeRecord, principal: Principal) -> bool:
    if record.tenant_id != principal.tenant_id or record.app_id != ASSISTANT_APP_ID:
        return False
    if principal.account_id and record.account_id != principal.account_id:
        return False
    if principal.app_id and principal.app_id != ASSISTANT_APP_ID:
        return False
    if principal.space_ids is not None and record.space_id not in principal.space_ids:
        return False
    if principal.purposes is not None and record.purpose not in principal.purposes:
        return False
    return True


# The assistant service surface returns raw record content, so it must apply the
# same ceiling the vector retrieval path does (see AccessFilter.allows): never
# hand a service key a confidential/restricted record, and never hand it anything
# that is not approved — pending/quarantined content is unvetted. INTERNAL is the
# assistant's own capture tier, so its approved internal context stays readable.
# NOTE: once dedicated assistant scopes (e.g. assistant:context:read) land, a plain
# read:public key should drop this ceiling to PUBLIC.
ASSISTANT_SERVICE_READ_CEILING = Classification.INTERNAL


def _service_readable(record: IntakeRecord) -> bool:
    if record.status != "approved":
        return False
    return Classification.parse(record.classification) <= ASSISTANT_SERVICE_READ_CEILING


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

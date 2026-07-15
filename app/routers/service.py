"""The service surface: how non-human callers push and pull data, and how an
admin mints the keys that authorise them.

Two disjoint capabilities, both narrow by construction:
  * write:capture -> POST /api/service/capture : content is CLAMPED to
    INTERNAL / captured_input (a compartment no read key and no ordinary staff
    role can see). A write key therefore cannot create anything world-readable.
  * read:public   -> POST /api/service/ask     : answered PUBLIC-ceiled, with
    sources stripped. A read key cannot retrieve anything above PUBLIC.

Key management (/api/service-keys) is human-admin-only.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.principal import Principal, resolve_principal, resolve_service_principal
from app.config import get_settings
from app.deps import (
    get_intake_pipeline, get_intake_store, get_job_store, get_kpi_store, get_pipeline, get_platform_store,
    get_retrieval_service, get_service_key_store, get_service_rate_limiter,
)
from app.intake.base import IntakeRecord
from app.intake.pipeline import IntakeInput
from app.jobs.base import JOB_SERVICE_CAPTURE, JOB_SERVICE_INTAKE
from app.kpis.base import (
    KPI_APP_ID, KPI_SNAPSHOT_WRITE_PURPOSE, KpiConflictError, KpiLimitError,
    KpiSnapshot, normalize_decimal, normalize_timestamp, now_iso,
)
from app.platform.base import AuditEvent, BrandTheme, normalize_unique, scope_is_held
from app.routers.jobs import job_status_out
from app.schemas import (
    JobStatusOut,
    BrandThemeOut, IntakeRecordOut, MintedKey, ServiceAskRequest, ServiceAskResponse, ServiceCapabilitiesResponse,
    ServiceCaptureRequest, ServiceIntakeRequest, ServiceIntakeResponse, ServiceKeyCreate, ServiceKeyInfo,
)
from app.security.policy import CAPTURED_CATEGORY
from app.servicekeys.base import (
    SCOPE_READ, SCOPE_WRITE, VALID_SCOPES, ServiceKey, generate_key, hash_secret,
)

service_router = APIRouter(prefix="/api/service", tags=["service"])
keys_router = APIRouter(prefix="/api/service-keys", tags=["service-keys"])


class ServiceBrandThemeUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    primary_color: str | None = Field(default=None, max_length=7)
    secondary_color: str | None = Field(default=None, max_length=7)
    accent_color: str | None = Field(default=None, max_length=7)
    background_color: str | None = Field(default=None, max_length=7)
    surface_color: str | None = Field(default=None, max_length=7)
    text_color: str | None = Field(default=None, max_length=7)
    muted_color: str | None = Field(default=None, max_length=7)
    success_color: str | None = Field(default=None, max_length=7)
    warning_color: str | None = Field(default=None, max_length=7)
    danger_color: str | None = Field(default=None, max_length=7)
    logo_url: str | None = Field(default=None, max_length=500)


class ServiceKpiSnapshotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kpi_id: str | None = Field(default=None, min_length=1, max_length=120)
    kpi_key: str | None = Field(default=None, min_length=2, max_length=64)
    value: Decimal
    observed_at: str = Field(min_length=1, max_length=80)
    source_ref: str = Field(default="", max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_reference(self):
        if bool(self.kpi_id) == bool(self.kpi_key):
            raise ValueError("Provide exactly one of kpi_id or kpi_key.")
        return self


class ServiceKpiSnapshotBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=120)
    snapshots: list[ServiceKpiSnapshotItem] = Field(min_length=1, max_length=100)


class ServiceKpiSnapshotOut(BaseModel):
    id: str
    kpi_id: str
    value: str
    observed_at: str
    received_at: str
    source_ref: str


class ServiceKpiIngestOut(BaseModel):
    accepted_count: int
    duplicate_count: int
    snapshots: list[ServiceKpiSnapshotOut]


def _platform_scope(body, principal: Principal, default_purpose: str):
    fields = {
        "account_id": (body.account_id or "").strip(),
        "space_id": (body.space_id or "").strip(),
        "app_id": (body.app_id or "").strip(),
        "purpose": (body.purpose or default_purpose).strip(),
    }
    if not fields["account_id"] and principal.account_id:
        fields["account_id"] = principal.account_id
    if not fields["app_id"] and principal.app_id:
        fields["app_id"] = principal.app_id
    if not fields["space_id"] and principal.space_ids and len(principal.space_ids) == 1:
        fields["space_id"] = next(iter(principal.space_ids))

    provided = [fields["account_id"], fields["space_id"], fields["app_id"]]
    if not any(provided):
        return None, principal
    if not all(provided):
        raise HTTPException(
            status_code=400,
            detail="account_id, space_id and app_id must be provided together for platform-scoped service calls.",
        )
    if fields["account_id"] != principal.tenant_id:
        raise HTTPException(status_code=403, detail="This service key is not pinned to that account.")
    if principal.account_id and fields["account_id"] != principal.account_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that account.")
    if principal.app_id and fields["app_id"] != principal.app_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that app.")
    if principal.space_ids is not None and fields["space_id"] not in principal.space_ids:
        raise HTTPException(status_code=403, detail="This service key cannot use that space.")
    if principal.purposes is not None and fields["purpose"] not in principal.purposes:
        raise HTTPException(status_code=403, detail="This service key cannot use that purpose.")

    store = get_platform_store()
    decision = store.check_app_access(
        fields["account_id"], fields["app_id"], fields["space_id"], fields["purpose"],
    )
    store.record_audit(AuditEvent(
        id=f"aud_{uuid4().hex}",
        account_id=fields["account_id"],
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action="service.access_checked",
        target_type="space",
        target_id=fields["space_id"],
        space_id=fields["space_id"],
        app_id=fields["app_id"],
        purpose=fields["purpose"],
        decision="allowed" if decision.allowed else "denied",
        meta={"reason": decision.reason},
    ))
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"App access denied: {decision.reason}")

    return fields, replace(
        principal,
        account_id=fields["account_id"],
        space_ids=frozenset({fields["space_id"]}),
    )


def _default_intake_purpose(app_id: str) -> str:
    if app_id == "communication":
        return "customer_service_inbox"
    if app_id == "assistant":
        return "assistant_action"
    return "knowledge_management"


def _preferred_space_kind(app_id: str, purpose: str, content: str) -> tuple[str, ...]:
    text = (content or "").lower()
    if purpose.startswith("customer_service") or app_id == "communication":
        return ("customer_service", "shared", "business")
    if app_id == "assistant":
        if any(word in text for word in ("family", "child", "home", "private")):
            return ("family", "personal", "shared", "business")
        if any(word in text for word in ("company", "client", "invoice", "project", "business")):
            return ("business", "shared", "personal")
        return ("personal", "shared", "business")
    return ("business", "shared", "customer_service", "personal")


def _route_intake_space(account_id: str, app_id: str, purpose: str, content: str, principal: Principal) -> str:
    if principal.space_ids and len(principal.space_ids) == 1:
        return next(iter(principal.space_ids))

    store = get_platform_store()
    allowed_ids = set(principal.space_ids or [])
    if not allowed_ids:
        for installation in store.list_app_installations(account_id):
            if installation.app_id == app_id and installation.status == "active":
                allowed_ids.update(installation.enabled_space_ids)
    spaces = [space for space in store.list_spaces(account_id) if space.id in allowed_ids and space.status == "active"]
    for kind in _preferred_space_kind(app_id, purpose, content):
        for space in spaces:
            if space.kind == kind:
                return space.id
    return spaces[0].id if spaces else ""


def _intake_scope(body: ServiceIntakeRequest, principal: Principal):
    fields = {
        "account_id": (body.account_id or principal.account_id or principal.tenant_id or "").strip(),
        "space_id": (body.space_id or "").strip(),
        "app_id": (body.app_id or principal.app_id or "").strip(),
        "purpose": (body.purpose or "").strip(),
    }
    if not fields["app_id"]:
        raise HTTPException(status_code=400, detail="app_id is required for service intake.")
    if not fields["purpose"]:
        fields["purpose"] = _default_intake_purpose(fields["app_id"])
    if not fields["space_id"]:
        fields["space_id"] = _route_intake_space(
            fields["account_id"], fields["app_id"], fields["purpose"], body.content, principal,
        )
    if not all(fields.values()):
        raise HTTPException(
            status_code=400,
            detail="account_id, space_id, app_id and purpose are required for service intake.",
        )
    if fields["account_id"] != principal.tenant_id:
        raise HTTPException(status_code=403, detail="This service key is not pinned to that account.")
    if principal.account_id and fields["account_id"] != principal.account_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that account.")
    if principal.app_id and fields["app_id"] != principal.app_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that app.")
    if principal.space_ids is not None and fields["space_id"] not in principal.space_ids:
        raise HTTPException(status_code=403, detail="This service key cannot use that space.")
    if principal.purposes is not None and fields["purpose"] not in principal.purposes:
        raise HTTPException(status_code=403, detail="This service key cannot use that purpose.")

    store = get_platform_store()
    decision = store.check_app_access(
        fields["account_id"], fields["app_id"], fields["space_id"], fields["purpose"],
    )
    store.record_audit(AuditEvent(
        id=f"aud_{uuid4().hex}",
        account_id=fields["account_id"],
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action="service.intake_access_checked",
        target_type="space",
        target_id=fields["space_id"],
        space_id=fields["space_id"],
        app_id=fields["app_id"],
        purpose=fields["purpose"],
        decision="allowed" if decision.allowed else "denied",
        meta={"reason": decision.reason},
    ))
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"App access denied: {decision.reason}")
    return fields


def _intake_record_out(record: IntakeRecord) -> IntakeRecordOut:
    return IntakeRecordOut(
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
        summary=record.summary,
        extracted_facts=record.extracted_facts,
        metadata=record.metadata,
        created_at=record.created_at,
    )


def _require_scope(principal: Principal, scope: str) -> None:
    if not principal.has_scope(scope):
        raise HTTPException(status_code=403, detail=f"This service key lacks the '{scope}' scope.")


def _rate_limit(principal: Principal) -> None:
    # Per-key limit on the metered endpoints, so a leaked key can't be looped for
    # unbounded LLM/embedding cost.
    wait = get_service_rate_limiter().check(principal.user_id)
    if wait > 0:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down.",
                            headers={"Retry-After": str(wait)})


def _service_key_info(k: ServiceKey) -> ServiceKeyInfo:
    return ServiceKeyInfo(
        id=k.id,
        tenant_id=k.tenant_id,
        scopes=list(k.scopes),
        label=k.label,
        account_id=k.account_id,
        app_id=k.app_id,
        space_ids=list(k.space_ids),
        purposes=list(k.purposes),
        status=k.status,
        last_used_at=k.last_used_at,
        last_used_endpoint=k.last_used_endpoint,
        use_count=k.use_count,
        rotated_from_id=k.rotated_from_id,
        revoked_at=k.revoked_at,
    )


def _brand_theme_out(theme) -> BrandThemeOut:
    return BrandThemeOut(
        id=theme.id,
        account_id=theme.account_id,
        app_id=theme.app_id,
        name=theme.name,
        primary_color=theme.primary_color,
        secondary_color=theme.secondary_color,
        accent_color=theme.accent_color,
        background_color=theme.background_color,
        surface_color=theme.surface_color,
        text_color=theme.text_color,
        muted_color=theme.muted_color,
        success_color=theme.success_color,
        warning_color=theme.warning_color,
        danger_color=theme.danger_color,
        logo_url=theme.logo_url,
        source=theme.source,
        status=theme.status,
        created_at=theme.created_at,
        updated_at=theme.updated_at,
    )


def _minted_key_out(k: ServiceKey, plaintext: str) -> MintedKey:
    return MintedKey(
        id=k.id,
        key=plaintext,
        tenant_id=k.tenant_id,
        scopes=list(k.scopes),
        label=k.label,
        account_id=k.account_id,
        app_id=k.app_id,
        space_ids=list(k.space_ids),
        purposes=list(k.purposes),
        rotated_from_id=k.rotated_from_id,
    )


def _key_audit_meta(k: ServiceKey, extra: dict | None = None) -> dict:
    meta = {
        "scopes": list(k.scopes),
        "label": k.label,
        "account_id": k.account_id,
        "app_id": k.app_id,
        "space_ids": list(k.space_ids),
        "purposes": list(k.purposes),
    }
    if extra:
        meta.update(extra)
    return meta


def _record_key_audit(action: str, principal: Principal, key: ServiceKey, extra: dict | None = None) -> None:
    account_id = key.account_id or key.tenant_id or principal.tenant_id
    get_platform_store().record_audit(AuditEvent(
        id=f"aud_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type="service_key",
        target_id=key.id,
        space_id=key.space_ids[0] if len(key.space_ids) == 1 else "",
        app_id=key.app_id,
        purpose=key.purposes[0] if len(key.purposes) == 1 else "",
        decision="recorded",
        meta=_key_audit_meta(key, extra),
    ))


# --- Service data surface (service-key auth) -----------------------------
@service_router.get("/capabilities", response_model=ServiceCapabilitiesResponse)
def capabilities(principal: Principal = Depends(resolve_service_principal)):
    from app.assistant.contracts import (
        ASSISTANT_CONTRACT_VERSION,
        ASSISTANT_INTENTS,
        ASSISTANT_RECORD_TYPES,
    )

    return ServiceCapabilitiesResponse(
        tenant_id=principal.tenant_id,
        account_id=principal.account_id,
        app_id=principal.app_id,
        scopes=sorted(principal.scopes),
        space_ids=sorted(principal.space_ids or []),
        purposes=sorted(principal.purposes or []),
        contract_version=ASSISTANT_CONTRACT_VERSION,
        record_types=sorted(ASSISTANT_RECORD_TYPES),
        intents=sorted(ASSISTANT_INTENTS),
    )


@service_router.get("/brand-theme", response_model=BrandThemeOut)
def service_brand_theme(principal: Principal = Depends(resolve_service_principal)):
    account_id = principal.account_id or principal.tenant_id
    return _brand_theme_out(get_platform_store().resolve_brand_theme(account_id, principal.app_id))


@service_router.put("/brand-theme", response_model=BrandThemeOut)
def update_service_brand_theme(
    body: ServiceBrandThemeUpdate,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_WRITE)
    if not principal.app_id:
        raise HTTPException(status_code=400, detail="An app-pinned service key is required to update a brand theme.")
    account_id = principal.account_id or principal.tenant_id
    store = get_platform_store()
    current = store.resolve_brand_theme(account_id, principal.app_id)
    try:
        theme = store.upsert_brand_theme(BrandTheme(
            id=f"brand_{account_id}_{principal.app_id}",
            account_id=account_id,
            app_id=principal.app_id,
            name=(body.name if body.name is not None else current.name).strip(),
            primary_color=body.primary_color or current.primary_color,
            secondary_color=body.secondary_color or current.secondary_color,
            accent_color=body.accent_color or current.accent_color,
            background_color=body.background_color or current.background_color,
            surface_color=body.surface_color or current.surface_color,
            text_color=body.text_color or current.text_color,
            muted_color=body.muted_color or current.muted_color,
            success_color=body.success_color or current.success_color,
            warning_color=body.warning_color or current.warning_color,
            danger_color=body.danger_color or current.danger_color,
            logo_url=(body.logo_url if body.logo_url is not None else current.logo_url).strip(),
            source="service_override",
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.record_audit(AuditEvent(
        id=f"aud_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action="brand_theme.updated",
        target_type="brand_theme",
        target_id=theme.id,
        app_id=theme.app_id,
        decision="recorded",
        meta={"source": theme.source},
    ))
    return _brand_theme_out(theme)


@service_router.post("/intake", response_model=ServiceIntakeResponse | JobStatusOut)
def intake(
    body: ServiceIntakeRequest,
    response: Response = None,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    fields = _intake_scope(body, principal)
    settings = get_settings()
    if settings.use_async_ingestion:
        job = get_job_store().enqueue(
            type=JOB_SERVICE_INTAKE,
            tenant_id=principal.tenant_id,
            account_id=fields["account_id"],
            space_id=fields["space_id"],
            requested_by=principal.user_id,
            payload={
                "app_id": fields["app_id"],
                "purpose": fields["purpose"],
                "content": body.content,
                "title": body.title or "",
                "source": body.source or "service",
                "source_ref": body.source_ref,
                "record_type": body.record_type,
                "intent": body.intent,
                "metadata": body.metadata,
            },
            max_attempts=settings.job_max_attempts,
        )
        if response is not None:
            response.status_code = 202
        return job_status_out(job)
    try:
        record = get_intake_pipeline().ingest(IntakeInput(
            tenant_id=principal.tenant_id,
            account_id=fields["account_id"],
            space_id=fields["space_id"],
            app_id=fields["app_id"],
            purpose=fields["purpose"],
            content=body.content,
            title=body.title or "",
            source=body.source or "service",
            source_ref=body.source_ref,
            record_type=body.record_type,
            intent=body.intent,
            metadata=body.metadata,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ServiceIntakeResponse(record=_intake_record_out(record))


class ServiceRecordDeleteRequest(BaseModel):
    source_ref: str = Field(min_length=1, max_length=500)
    account_id: str = Field(default="", max_length=120)
    space_id: str = Field(default="", max_length=120)


class ServiceRecordDeleteResponse(BaseModel):
    source_ref: str
    deleted: int
    audit_event_id: str = ""


@service_router.post("/records/delete", response_model=ServiceRecordDeleteResponse)
def delete_records(
    body: ServiceRecordDeleteRequest,
    principal: Principal = Depends(resolve_service_principal),
):
    """Module-initiated erasure of a record the module previously synced in.

    The module names the record by the same source_ref it used at intake, so a
    contact/tenant deletion inside a module can erase the canonical copy here.
    Idempotent (an unknown ref deletes nothing) and refused under a legal hold."""
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)

    account_id = (body.account_id or principal.account_id or principal.tenant_id).strip()
    space_id = (body.space_id or "").strip()
    if account_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="This service key is not pinned to that account.")
    if principal.account_id and account_id != principal.account_id:
        raise HTTPException(status_code=403, detail="This service key cannot use that account.")
    if space_id and principal.space_ids is not None and space_id not in principal.space_ids:
        raise HTTPException(status_code=403, detail="This service key cannot use that space.")

    source_ref = body.source_ref.strip()
    store = get_platform_store()

    # Legal hold beats module-initiated erasure, exactly as it beats a human erase.
    if scope_is_held(store.list_legal_holds(account_id), space_id):
        store.record_audit(AuditEvent(
            id=f"aud_{uuid4().hex}",
            account_id=account_id,
            actor_id=principal.user_id,
            actor_type=principal.principal_type,
            action="service.records.delete_denied",
            target_type="space" if space_id else "account",
            target_id=space_id or account_id,
            space_id=space_id,
            app_id=principal.app_id,
            purpose="gdpr_delete",
            decision="denied_legal_hold",
            meta={"source_ref": source_ref},
        ))
        raise HTTPException(
            status_code=409,
            detail="This scope is under an active legal hold; the record cannot be erased.",
        )

    deleted = get_intake_store().delete_by_source_ref(
        principal.tenant_id, source_ref, account_id=account_id, space_id=space_id,
    )
    audit = store.record_audit(AuditEvent(
        id=f"aud_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action="service.records.deleted",
        target_type="space" if space_id else "account",
        target_id=space_id or account_id,
        space_id=space_id,
        app_id=principal.app_id,
        purpose="gdpr_delete",
        decision="completed",
        meta={"source_ref": source_ref, "deleted": deleted},
    ))
    return ServiceRecordDeleteResponse(source_ref=source_ref, deleted=deleted, audit_event_id=audit.id)


class ServiceTombstoneOut(BaseModel):
    id: str
    seq: int
    account_id: str
    space_id: str = ""
    target_type: str
    target_ref: str = ""
    reason: str = ""
    created_at: str = ""


class ServiceTombstoneFeedOut(BaseModel):
    tombstones: list[ServiceTombstoneOut]
    cursor: int


class ServiceTombstoneAckOut(BaseModel):
    tombstone_id: str
    app_id: str
    acked_at: str = ""


@service_router.get("/tombstones", response_model=ServiceTombstoneFeedOut)
def list_tombstones(
    since: int = 0,
    limit: int = 100,
    principal: Principal = Depends(resolve_service_principal),
):
    """The erasure feed. A module polls forward from `cursor` and mirrors each
    tombstone, then acks it. Operational deletion instructions, not public content,
    so it takes the write scope like the module's other mutating calls."""
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    account_id = principal.account_id or principal.tenant_id
    rows = get_platform_store().list_tombstones(
        account_id, since_seq=max(0, since), limit=min(max(1, limit), 500),
    )
    out = [
        ServiceTombstoneOut(
            id=row.id, seq=row.seq, account_id=row.account_id, space_id=row.space_id,
            target_type=row.target_type, target_ref=row.target_ref, reason=row.reason,
            created_at=row.created_at,
        )
        for row in rows
    ]
    cursor = max((row.seq for row in rows), default=max(0, since))
    return ServiceTombstoneFeedOut(tombstones=out, cursor=cursor)


@service_router.post("/tombstones/{tombstone_id}/ack", response_model=ServiceTombstoneAckOut)
def ack_tombstone(
    tombstone_id: str,
    principal: Principal = Depends(resolve_service_principal),
):
    """Record that this module has applied a tombstone. Idempotent."""
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    account_id = principal.account_id or principal.tenant_id
    app_id = principal.app_id or "unknown"
    ack = get_platform_store().ack_tombstone(tombstone_id, app_id, account_id=account_id)
    if not ack:
        raise HTTPException(status_code=404, detail="No such tombstone for this account.")
    return ServiceTombstoneAckOut(
        tombstone_id=ack.tombstone_id, app_id=ack.app_id, acked_at=ack.acked_at,
    )


@service_router.post("/capture")
def capture(
    body: ServiceCaptureRequest,
    response: Response = None,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    platform_scope, _ = _platform_scope(body, principal, "customer_service_inbox")
    settings = get_settings()
    if settings.use_async_ingestion:
        job = get_job_store().enqueue(
            type=JOB_SERVICE_CAPTURE,
            tenant_id=principal.tenant_id,
            account_id=platform_scope["account_id"] if platform_scope else "",
            space_id=platform_scope["space_id"] if platform_scope else "",
            requested_by=principal.user_id,
            payload={
                "title": body.title or "captured message",
                "text": body.text,
                "pii_phase": settings.pii_phase,
            },
            max_attempts=settings.job_max_attempts,
        )
        if response is not None:
            response.status_code = 202
        return job_status_out(job)
    try:
        # Labels are CLAMPED here, not taken from the caller: a service write can
        # only ever land as INTERNAL/captured_input in its own tenant.
        result = get_pipeline().ingest_text(
            title=body.title or "captured message",
            text=body.text,
            classification="internal",
            location="global",
            category=CAPTURED_CATEGORY,
            uploaded_by=principal.user_id,
            tenant=principal.tenant_id,
            require_approval=False,
            block_public_on_pii=False,      # not public; the compartment is the control
            pii_phase=settings.pii_phase,   # still refuse real PII before the DPIA
            account_id=platform_scope["account_id"] if platform_scope else "",
            space_id=platform_scope["space_id"] if platform_scope else "",
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"captured": result.doc_id, "chunks": result.chunks}


@service_router.post("/kpis/snapshots", response_model=ServiceKpiIngestOut)
def write_kpi_snapshots(
    body: ServiceKpiSnapshotBatch,
    principal: Principal = Depends(resolve_service_principal),
):
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    account_id = principal.account_id
    space_id = body.space_id.strip()
    if not account_id or account_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="This service key is not pinned to an account.")
    if principal.app_id != KPI_APP_ID:
        raise HTTPException(status_code=403, detail="This service key cannot ingest KPI snapshots.")
    if principal.space_ids is None or space_id not in principal.space_ids:
        raise HTTPException(status_code=403, detail="This service key cannot use that space.")
    if principal.purposes is None or KPI_SNAPSHOT_WRITE_PURPOSE not in principal.purposes:
        raise HTTPException(status_code=403, detail="This service key cannot write KPI snapshots.")

    platform = get_platform_store()
    decision = platform.check_app_access(
        account_id, KPI_APP_ID, space_id, KPI_SNAPSHOT_WRITE_PURPOSE,
    )
    if not decision.allowed:
        _record_kpi_batch_audit(
            principal,
            account_id=account_id,
            space_id=space_id,
            decision="denied",
            meta={"reason": decision.reason, "item_count": len(body.snapshots)},
        )
        raise HTTPException(status_code=403, detail="KPI Dashboard is not enabled for this workspace.")

    store = get_kpi_store()
    received_at = now_iso()
    snapshots: list[KpiSnapshot] = []
    kpi_ids: list[str] = []
    try:
        for item in body.snapshots:
            definition = (
                store.get_definition(
                    item.kpi_id or "", account_id=account_id, space_id=space_id,
                )
                if item.kpi_id
                else store.get_definition_by_key(
                    item.kpi_key or "", account_id=account_id, space_id=space_id,
                )
            )
            if not definition or definition.status != "active":
                raise ValueError("KPI definition not found in the authorized workspace.")
            kpi_ids.append(definition.id)
            snapshots.append(KpiSnapshot(
                id=f"kpisnap_{uuid4().hex}",
                account_id=account_id,
                space_id=space_id,
                kpi_id=definition.id,
                value=normalize_decimal(item.value),
                observed_at=normalize_timestamp(item.observed_at),
                received_at=received_at,
                source_ref=item.source_ref.strip(),
                idempotency_key=item.idempotency_key.strip(),
                created_by=principal.user_id,
            ))
        result = store.ingest_snapshots(snapshots)
    except (KpiConflictError, KpiLimitError) as exc:
        _record_kpi_batch_audit(
            principal,
            account_id=account_id,
            space_id=space_id,
            decision="rejected",
            meta={"reason": type(exc).__name__, "item_count": len(body.snapshots)},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        _record_kpi_batch_audit(
            principal,
            account_id=account_id,
            space_id=space_id,
            decision="rejected",
            meta={"reason": "validation_failed", "item_count": len(body.snapshots)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _record_kpi_batch_audit(
        principal,
        account_id=account_id,
        space_id=space_id,
        decision="recorded",
        meta={
            "kpi_ids": sorted(set(kpi_ids)),
            "snapshot_ids": [row.id for row in result.snapshots],
            "accepted_count": result.accepted_count,
            "duplicate_count": result.duplicate_count,
        },
    )
    return ServiceKpiIngestOut(
        accepted_count=result.accepted_count,
        duplicate_count=result.duplicate_count,
        snapshots=[ServiceKpiSnapshotOut(
            id=row.id,
            kpi_id=row.kpi_id,
            value=str(row.value),
            observed_at=row.observed_at,
            received_at=row.received_at,
            source_ref=row.source_ref,
        ) for row in result.snapshots],
    )


def _record_kpi_batch_audit(
    principal: Principal,
    *,
    account_id: str,
    space_id: str,
    decision: str,
    meta: dict,
) -> None:
    get_platform_store().record_audit(AuditEvent(
        id=f"aud_kpi_batch_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action="kpi_snapshots.ingested",
        target_type="kpi_snapshot_batch",
        target_id=space_id,
        space_id=space_id,
        app_id=KPI_APP_ID,
        purpose=KPI_SNAPSHOT_WRITE_PURPOSE,
        decision=decision,
        meta=meta,
    ))


@service_router.post("/ask", response_model=ServiceAskResponse)
def service_ask(body: ServiceAskRequest, principal: Principal = Depends(resolve_service_principal)):
    _require_scope(principal, SCOPE_READ)
    _rate_limit(principal)
    _, scoped_principal = _platform_scope(body, principal, "customer_service_answer")
    service = get_retrieval_service()
    answer_parts: list[str] = []
    meta: dict = {}
    for event in service.answer_stream(scoped_principal, body.question):
        if event["type"] == "token":
            answer_parts.append(event["text"])
        elif event["type"] == "meta":
            meta = event
    # No sources are returned to a service principal (also stripped brain-side).
    return ServiceAskResponse(answer="".join(answer_parts), chunks_used=meta.get("chunks_used", 0))


# --- Key management (human admin only) -----------------------------------
def _require_admin(principal: Principal) -> None:
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can manage service keys.")


@keys_router.post("", response_model=MintedKey)
def mint_key(body: ServiceKeyCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    scopes = tuple(s for s in dict.fromkeys(body.scopes) if s in VALID_SCOPES)
    if not scopes:
        raise HTTPException(status_code=400, detail=f"Provide at least one valid scope: {sorted(VALID_SCOPES)}.")
    app_id = (body.app_id or "").strip()
    space_ids = normalize_unique(body.space_ids)
    purposes = normalize_unique(body.purposes)
    account_id = principal.tenant_id if app_id or space_ids or purposes else ""
    if (space_ids or purposes) and not app_id:
        raise HTTPException(status_code=400, detail="app_id is required when space_ids or purposes are constrained.")
    # Cap the active-key surface per tenant.
    active = [k for k in get_service_key_store().list_by_tenant(principal.tenant_id) if k.status == "active"]
    if len(active) >= get_settings().max_service_keys_per_tenant:
        raise HTTPException(status_code=409, detail="This tenant already holds the maximum number of active service keys.")
    key_id, secret, plaintext = generate_key()
    # A key is minted for the admin's OWN tenant — no cross-tenant minting.
    key = get_service_key_store().create(ServiceKey(
        id=key_id, key_hash=hash_secret(secret), tenant_id=principal.tenant_id,
        scopes=scopes, label=body.label or "", account_id=account_id, app_id=app_id,
        space_ids=space_ids, purposes=purposes,
    ))
    _record_key_audit("service_key.minted", principal, key)
    return _minted_key_out(key, plaintext)


@keys_router.get("", response_model=list[ServiceKeyInfo])
def list_keys(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_service_key_info(k) for k in get_service_key_store().list_by_tenant(principal.tenant_id)]


@keys_router.delete("/{key_id}")
def revoke_key(key_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    key = get_service_key_store().get(key_id)
    # Tenant-scoped: an admin can only revoke keys in their own tenant.
    if not key or key.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Service key not found.")
    get_service_key_store().revoke(key_id)
    _record_key_audit("service_key.revoked", principal, key)
    return {"revoked": key_id}


@keys_router.post("/{key_id}/rotate", response_model=MintedKey)
def rotate_key(key_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    old = get_service_key_store().get(key_id)
    if not old or old.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Service key not found.")
    if old.status != "active":
        raise HTTPException(status_code=409, detail="Only active service keys can be rotated.")
    new_key_id, secret, plaintext = generate_key()
    try:
        rotated = get_service_key_store().rotate(
            old.id,
            ServiceKey(
                id=new_key_id,
                key_hash=hash_secret(secret),
                tenant_id=old.tenant_id,
                scopes=old.scopes,
                label=old.label,
                account_id=old.account_id,
                app_id=old.app_id,
                space_ids=old.space_ids,
                purposes=old.purposes,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_key_audit(
        "service_key.rotated",
        principal,
        rotated,
        extra={"old_key_id": old.id, "new_key_id": rotated.id},
    )
    return _minted_key_out(rotated, plaintext)

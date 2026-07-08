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
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.auth.principal import Principal, resolve_principal, resolve_service_principal
from app.config import get_settings
from app.deps import (
    get_pipeline, get_platform_store, get_retrieval_service, get_service_key_store, get_service_rate_limiter,
)
from app.platform.base import AuditEvent, normalize_unique
from app.schemas import (
    MintedKey, ServiceAskRequest, ServiceAskResponse, ServiceCaptureRequest,
    ServiceKeyCreate, ServiceKeyInfo,
)
from app.security.policy import CAPTURED_CATEGORY
from app.servicekeys.base import (
    SCOPE_READ, SCOPE_WRITE, VALID_SCOPES, ServiceKey, generate_key, hash_secret,
)

service_router = APIRouter(prefix="/api/service", tags=["service"])
keys_router = APIRouter(prefix="/api/service-keys", tags=["service-keys"])


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


# --- Service data surface (service-key auth) -----------------------------
@service_router.post("/capture")
def capture(body: ServiceCaptureRequest, principal: Principal = Depends(resolve_service_principal)):
    _require_scope(principal, SCOPE_WRITE)
    _rate_limit(principal)
    platform_scope, _ = _platform_scope(body, principal, "customer_service_inbox")
    settings = get_settings()
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
    get_service_key_store().create(ServiceKey(
        id=key_id, key_hash=hash_secret(secret), tenant_id=principal.tenant_id,
        scopes=scopes, label=body.label or "", account_id=account_id, app_id=app_id,
        space_ids=space_ids, purposes=purposes,
    ))
    return MintedKey(id=key_id, key=plaintext, tenant_id=principal.tenant_id,
                     scopes=list(scopes), label=body.label or "", account_id=account_id,
                     app_id=app_id, space_ids=list(space_ids), purposes=list(purposes))


@keys_router.get("", response_model=list[ServiceKeyInfo])
def list_keys(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [
        ServiceKeyInfo(
            id=k.id, tenant_id=k.tenant_id, scopes=list(k.scopes), label=k.label,
            account_id=k.account_id, app_id=k.app_id, space_ids=list(k.space_ids),
            purposes=list(k.purposes), status=k.status,
        )
        for k in get_service_key_store().list_by_tenant(principal.tenant_id)
    ]


@keys_router.delete("/{key_id}")
def revoke_key(key_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    key = get_service_key_store().get(key_id)
    # Tenant-scoped: an admin can only revoke keys in their own tenant.
    if not key or key.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Service key not found.")
    get_service_key_store().revoke(key_id)
    return {"revoked": key_id}

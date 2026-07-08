"""Platform foundation endpoints: accounts, spaces, apps, access and audit."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.principal import Principal, resolve_principal
from app.deps import get_platform_store
from app.platform.base import Account, AppInstallation, AuditEvent, BrandTheme, DEFAULT_BRAND_THEME, Space, normalize_unique
from app.schemas import BrandThemeOut

router = APIRouter(prefix="/api/platform", tags=["platform"])


class AccountCreate(BaseModel):
    kind: str = Field(pattern="^(person|organization|family|project)$")
    name: str = Field(min_length=1, max_length=200)
    id: str | None = Field(default=None, max_length=120)


class AccountOut(BaseModel):
    id: str
    kind: str
    name: str
    owner_user_id: str = ""
    status: str = "active"


class SpaceCreate(BaseModel):
    kind: str = Field(pattern="^(personal|business|customer_service|shared|family|project)$")
    name: str = Field(min_length=1, max_length=200)
    id: str | None = Field(default=None, max_length=120)


class SpaceOut(BaseModel):
    id: str
    account_id: str
    kind: str
    name: str
    status: str = "active"


class AppInstallCreate(BaseModel):
    app_id: str = Field(pattern="^(onebrain_core|assistant|communication|admin_console|workers)$")
    enabled_space_ids: list[str] = Field(default_factory=list)
    allowed_purposes: list[str] = Field(default_factory=list)
    display_name: str = Field(default="", max_length=200)
    id: str | None = Field(default=None, max_length=120)


class AppInstallationOut(BaseModel):
    id: str
    account_id: str
    app_id: str
    enabled_space_ids: list[str]
    allowed_purposes: list[str]
    display_name: str = ""
    status: str = "active"


class BrandThemeInput(BaseModel):
    app_id: str = Field(default="", max_length=80)
    name: str = Field(default="", max_length=200)
    primary_color: str = Field(default=DEFAULT_BRAND_THEME["primary_color"], max_length=7)
    secondary_color: str = Field(default=DEFAULT_BRAND_THEME["secondary_color"], max_length=7)
    accent_color: str = Field(default=DEFAULT_BRAND_THEME["accent_color"], max_length=7)
    background_color: str = Field(default=DEFAULT_BRAND_THEME["background_color"], max_length=7)
    surface_color: str = Field(default=DEFAULT_BRAND_THEME["surface_color"], max_length=7)
    text_color: str = Field(default=DEFAULT_BRAND_THEME["text_color"], max_length=7)
    muted_color: str = Field(default=DEFAULT_BRAND_THEME["muted_color"], max_length=7)
    success_color: str = Field(default=DEFAULT_BRAND_THEME["success_color"], max_length=7)
    warning_color: str = Field(default=DEFAULT_BRAND_THEME["warning_color"], max_length=7)
    danger_color: str = Field(default=DEFAULT_BRAND_THEME["danger_color"], max_length=7)
    logo_url: str = Field(default="", max_length=500)


class AccessCheckRequest(BaseModel):
    account_id: str
    app_id: str
    space_id: str
    purpose: str


class AccessCheckResponse(BaseModel):
    allowed: bool
    reason: str


class AuditOut(BaseModel):
    id: str
    account_id: str
    actor_id: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    space_id: str = ""
    app_id: str = ""
    purpose: str = ""
    decision: str = ""
    meta: dict = Field(default_factory=dict)


def _require_admin(principal: Principal) -> None:
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can manage platform setup.")


def _account_out(account: Account) -> AccountOut:
    return AccountOut(id=account.id, kind=account.kind, name=account.name,
                      owner_user_id=account.owner_user_id, status=account.status)


def _space_out(space: Space) -> SpaceOut:
    return SpaceOut(id=space.id, account_id=space.account_id, kind=space.kind,
                    name=space.name, status=space.status)


def _app_out(installation: AppInstallation) -> AppInstallationOut:
    return AppInstallationOut(
        id=installation.id, account_id=installation.account_id, app_id=installation.app_id,
        enabled_space_ids=list(installation.enabled_space_ids),
        allowed_purposes=list(installation.allowed_purposes),
        display_name=installation.display_name, status=installation.status,
    )


def _brand_theme_out(theme: BrandTheme) -> BrandThemeOut:
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


def _audit(actor: Principal, action: str, target_type: str, target_id: str, account_id: str,
           *, space_id: str = "", app_id: str = "", purpose: str = "", decision: str = "", meta: dict | None = None):
    return AuditEvent(
        id=f"aud_{uuid4().hex}", account_id=account_id, actor_id=actor.user_id,
        actor_type=actor.principal_type, action=action, target_type=target_type, target_id=target_id,
        space_id=space_id, app_id=app_id, purpose=purpose, decision=decision, meta=meta or {},
    )


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_account_out(a) for a in get_platform_store().list_accounts()]


@router.post("/accounts", response_model=AccountOut)
def create_account(body: AccountCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_platform_store()
    account_id = body.id or f"acct_{uuid4().hex[:12]}"
    try:
        account = store.create_account(Account(
            id=account_id, kind=body.kind, name=body.name.strip(), owner_user_id=principal.user_id,
        ))
        store.record_audit(_audit(principal, "account.created", "account", account.id, account.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _account_out(account)


@router.get("/accounts/{account_id}/spaces", response_model=list[SpaceOut])
def list_spaces(account_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_space_out(s) for s in get_platform_store().list_spaces(account_id)]


@router.post("/accounts/{account_id}/spaces", response_model=SpaceOut)
def create_space(account_id: str, body: SpaceCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_platform_store()
    space_id = body.id or f"spc_{uuid4().hex[:12]}"
    try:
        space = store.create_space(Space(id=space_id, account_id=account_id, kind=body.kind, name=body.name.strip()))
        store.record_audit(_audit(principal, "space.created", "space", space.id, account_id, space_id=space.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _space_out(space)


@router.get("/accounts/{account_id}/apps", response_model=list[AppInstallationOut])
def list_apps(account_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_app_out(i) for i in get_platform_store().list_app_installations(account_id)]


@router.post("/accounts/{account_id}/apps", response_model=AppInstallationOut)
def install_app(account_id: str, body: AppInstallCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_platform_store()
    installation_id = body.id or f"appi_{uuid4().hex[:12]}"
    try:
        installation = store.install_app(AppInstallation(
            id=installation_id, account_id=account_id, app_id=body.app_id,
            enabled_space_ids=normalize_unique(body.enabled_space_ids),
            allowed_purposes=normalize_unique(body.allowed_purposes),
            display_name=body.display_name.strip(),
        ))
        store.record_audit(_audit(
            principal, "app.installed", "app_installation", installation.id, account_id,
            app_id=installation.app_id, meta={
                "enabled_space_ids": list(installation.enabled_space_ids),
                "allowed_purposes": list(installation.allowed_purposes),
            },
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _app_out(installation)


@router.get("/accounts/{account_id}/brand-themes", response_model=list[BrandThemeOut])
def list_brand_themes(account_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_brand_theme_out(theme) for theme in get_platform_store().list_brand_themes(account_id)]


@router.get("/accounts/{account_id}/brand-theme", response_model=BrandThemeOut)
def get_brand_theme(
    account_id: str,
    app_id: str = Query(default="", max_length=80),
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    store = get_platform_store()
    if not store.get_account(account_id):
        raise HTTPException(status_code=404, detail="Account not found.")
    return _brand_theme_out(store.resolve_brand_theme(account_id, app_id))


@router.put("/accounts/{account_id}/brand-theme", response_model=BrandThemeOut)
def upsert_brand_theme(account_id: str, body: BrandThemeInput, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_platform_store()
    if not store.get_account(account_id):
        raise HTTPException(status_code=404, detail="Account not found.")
    app_id = body.app_id.strip()
    try:
        theme = store.upsert_brand_theme(BrandTheme(
            id=f"brand_{account_id}_{app_id or 'account'}",
            account_id=account_id,
            app_id=app_id,
            name=body.name.strip(),
            primary_color=body.primary_color,
            secondary_color=body.secondary_color,
            accent_color=body.accent_color,
            background_color=body.background_color,
            surface_color=body.surface_color,
            text_color=body.text_color,
            muted_color=body.muted_color,
            success_color=body.success_color,
            warning_color=body.warning_color,
            danger_color=body.danger_color,
            logo_url=body.logo_url.strip(),
            source="operator",
        ))
        store.record_audit(_audit(
            principal,
            "brand_theme.updated",
            "brand_theme",
            theme.id,
            account_id,
            app_id=theme.app_id,
            meta={"source": theme.source},
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _brand_theme_out(theme)


@router.post("/access/check", response_model=AccessCheckResponse)
def check_access(body: AccessCheckRequest, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_platform_store()
    decision = store.check_app_access(body.account_id, body.app_id, body.space_id, body.purpose)
    store.record_audit(_audit(
        principal, "access.checked", "space", body.space_id, body.account_id,
        space_id=body.space_id, app_id=body.app_id, purpose=body.purpose,
        decision="allowed" if decision.allowed else "denied", meta={"reason": decision.reason},
    ))
    return AccessCheckResponse(allowed=decision.allowed, reason=decision.reason)


@router.get("/accounts/{account_id}/audit", response_model=list[AuditOut])
def list_audit(account_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [
        AuditOut(
            id=e.id, account_id=e.account_id, actor_id=e.actor_id, actor_type=e.actor_type,
            action=e.action, target_type=e.target_type, target_id=e.target_id,
            space_id=e.space_id, app_id=e.app_id, purpose=e.purpose, decision=e.decision, meta=e.meta,
        )
        for e in get_platform_store().list_audit(account_id)
    ]

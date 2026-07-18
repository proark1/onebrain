"""Platform foundation endpoints: accounts, spaces, apps, access and audit."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.account_access import authorize_account_admin, authorized_account_ids
from app.auth.principal import Principal, resolve_principal
from app.deps import get_platform_store
from app.platform.base import (
    AccessGroup,
    AccessGroupMembership,
    Account,
    AppInstallation,
    AuditEvent,
    BrandTheme,
    ConsentRecord,
    CredentialMetadata,
    DEFAULT_BRAND_THEME,
    DataAccessEvent,
    Membership,
    Organization,
    ProcessorRegistration,
    ProviderRegistration,
    RetentionPolicy,
    Space,
    normalize_unique,
)
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
    app_id: str = Field(pattern="^(onebrain_core|assistant|ai_employees|communication|kpi_dashboard|admin_console|workers)$")
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


class OrganizationIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    status: str = Field(default="active", max_length=40)


class OrganizationOut(BaseModel):
    id: str
    account_id: str
    name: str
    status: str = "active"
    created_at: str = ""


class MembershipIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    role_id: str = Field(min_length=1, max_length=80)
    space_id: str = Field(default="", max_length=120)
    organization_id: str = Field(default="", max_length=120)
    status: str = Field(default="active", max_length=40)


class MembershipOut(BaseModel):
    id: str
    account_id: str
    user_id: str
    role_id: str
    space_id: str = ""
    organization_id: str = ""
    status: str = "active"
    created_at: str = ""


class AccessGroupIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="department", pattern="^(department|team)$")
    space_id: str = Field(default="", max_length=120)
    status: str = Field(default="active", pattern="^(active|archived)$")


class AccessGroupOut(BaseModel):
    id: str
    account_id: str
    name: str
    kind: str
    space_id: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class AccessGroupMembershipIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    group_id: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    space_id: str = Field(default="", max_length=120)
    status: str = Field(default="active", pattern="^(active|inactive)$")


class AccessGroupMembershipOut(BaseModel):
    id: str
    account_id: str
    group_id: str
    user_id: str
    space_id: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class ConsentIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    subject_ref: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=80)
    status: str = Field(default="granted", max_length=40)
    space_id: str = Field(default="", max_length=120)
    source: str = Field(default="", max_length=200)
    withdrawn_at: str = Field(default="", max_length=80)


class ConsentOut(BaseModel):
    id: str
    account_id: str
    subject_ref: str
    purpose: str
    status: str
    space_id: str = ""
    source: str = ""
    captured_by: str = ""
    withdrawn_at: str = ""
    created_at: str = ""


class RetentionPolicyIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    domain: str = Field(min_length=1, max_length=80)
    record_type: str = Field(default="", max_length=80)
    action: str = Field(default="delete", max_length=40)
    duration_days: int = Field(ge=0)
    legal_basis: str = Field(default="", max_length=200)
    space_id: str = Field(default="", max_length=120)
    status: str = Field(default="active", max_length=40)


class RetentionPolicyOut(BaseModel):
    id: str
    account_id: str
    domain: str
    record_type: str
    action: str
    duration_days: int
    legal_basis: str
    space_id: str = ""
    status: str = "active"
    created_at: str = ""


class DataAccessEventIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    actor_id: str = Field(min_length=1, max_length=200)
    actor_type: str = Field(default="service", max_length=80)
    action: str = Field(min_length=1, max_length=120)
    target_type: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=200)
    space_id: str = Field(default="", max_length=120)
    app_id: str = Field(default="", max_length=80)
    purpose: str = Field(default="", max_length=80)
    decision: str = Field(default="", max_length=80)
    meta: dict = Field(default_factory=dict)


class DataAccessEventOut(DataAccessEventIn):
    id: str
    account_id: str
    created_at: str = ""


class ProcessorIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=120)
    region: str = Field(default="", max_length=120)
    dpa_status: str = Field(default="", max_length=120)
    transfer_mechanism: str = Field(default="", max_length=200)
    account_id: str = Field(default="", max_length=120)
    status: str = Field(default="active", max_length=40)
    meta: dict = Field(default_factory=dict)


class ProcessorOut(ProcessorIn):
    id: str
    created_at: str = ""


class ProviderIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=120)
    region: str = Field(default="", max_length=120)
    dpia_status: str = Field(default="", max_length=120)
    transfer_mechanism: str = Field(default="", max_length=200)
    account_id: str = Field(default="", max_length=120)
    status: str = Field(default="active", max_length=40)
    meta: dict = Field(default_factory=dict)


class ProviderOut(ProviderIn):
    id: str
    created_at: str = ""


class CredentialMetadataIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    provider: str = Field(min_length=1, max_length=120)
    app_id: str = Field(default="", max_length=80)
    secret_ref: str = Field(min_length=1, max_length=300)
    status: str = Field(default="active", max_length=40)
    rotated_at: str = Field(default="", max_length=80)
    last_verified_at: str = Field(default="", max_length=80)
    meta: dict = Field(default_factory=dict)


class CredentialMetadataOut(CredentialMetadataIn):
    id: str
    account_id: str
    created_at: str = ""


def _require_platform_admin(principal: Principal) -> None:
    """Role gate for operator-level, non-account-addressable routes (list/create
    accounts, the global DPA processor/provider registry). Account-addressable
    routes use authorize_account_admin, which additionally checks the caller is
    authorized for that specific account."""
    if principal.principal_type != "human" or principal.role_id != "admin":
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
    _require_platform_admin(principal)
    store = get_platform_store()
    allowed = authorized_account_ids(principal, store)
    return [_account_out(a) for a in store.list_accounts() if a.id in allowed]


@router.post("/accounts", response_model=AccountOut)
def create_account(body: AccountCreate, principal: Principal = Depends(resolve_principal)):
    _require_platform_admin(principal)
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
    authorize_account_admin(principal, account_id, get_platform_store())
    return [_space_out(s) for s in get_platform_store().list_spaces(account_id)]


@router.post("/accounts/{account_id}/spaces", response_model=SpaceOut)
def create_space(account_id: str, body: SpaceCreate, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
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
    authorize_account_admin(principal, account_id, get_platform_store())
    return [_app_out(i) for i in get_platform_store().list_app_installations(account_id)]


@router.post("/accounts/{account_id}/apps", response_model=AppInstallationOut)
def install_app(account_id: str, body: AppInstallCreate, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
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
    authorize_account_admin(principal, account_id, get_platform_store())
    return [_brand_theme_out(theme) for theme in get_platform_store().list_brand_themes(account_id)]


@router.get("/accounts/{account_id}/brand-theme", response_model=BrandThemeOut)
def get_brand_theme(
    account_id: str,
    app_id: str = Query(default="", max_length=80),
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    store = get_platform_store()
    if not store.get_account(account_id):
        raise HTTPException(status_code=404, detail="Account not found.")
    return _brand_theme_out(store.resolve_brand_theme(account_id, app_id))


@router.put("/accounts/{account_id}/brand-theme", response_model=BrandThemeOut)
def upsert_brand_theme(account_id: str, body: BrandThemeInput, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
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
    store = get_platform_store()
    authorize_account_admin(principal, body.account_id, store)
    decision = store.check_app_access(body.account_id, body.app_id, body.space_id, body.purpose)
    store.record_audit(_audit(
        principal, "access.checked", "space", body.space_id, body.account_id,
        space_id=body.space_id, app_id=body.app_id, purpose=body.purpose,
        decision="allowed" if decision.allowed else "denied", meta={"reason": decision.reason},
    ))
    return AccessCheckResponse(allowed=decision.allowed, reason=decision.reason)


@router.get("/accounts/{account_id}/audit", response_model=list[AuditOut])
def list_audit(account_id: str, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [
        AuditOut(
            id=e.id, account_id=e.account_id, actor_id=e.actor_id, actor_type=e.actor_type,
            action=e.action, target_type=e.target_type, target_id=e.target_id,
            space_id=e.space_id, app_id=e.app_id, purpose=e.purpose, decision=e.decision, meta=e.meta,
        )
        for e in get_platform_store().list_audit(account_id)
    ]


@router.get("/accounts/{account_id}/organizations", response_model=list[OrganizationOut])
def list_organizations(account_id: str, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [OrganizationOut(**org.__dict__) for org in get_platform_store().list_organizations(account_id)]


@router.post("/accounts/{account_id}/organizations", response_model=OrganizationOut)
def upsert_organization(account_id: str, body: OrganizationIn, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    org = Organization(id=body.id or f"org_{uuid4().hex[:12]}", account_id=account_id, name=body.name.strip(), status=body.status)
    try:
        saved = get_platform_store().upsert_organization(org)
        get_platform_store().record_audit(_audit(principal, "organization.upserted", "organization", saved.id, account_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationOut(**saved.__dict__)


@router.get("/accounts/{account_id}/memberships", response_model=list[MembershipOut])
def list_memberships(account_id: str, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [MembershipOut(**row.__dict__) for row in get_platform_store().list_memberships(account_id)]


@router.post("/accounts/{account_id}/memberships", response_model=MembershipOut)
def upsert_membership(account_id: str, body: MembershipIn, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    membership = Membership(
        id=body.id or f"mem_{uuid4().hex[:12]}",
        account_id=account_id,
        user_id=body.user_id.strip(),
        role_id=body.role_id.strip(),
        space_id=body.space_id.strip(),
        organization_id=body.organization_id.strip(),
        status=body.status,
    )
    try:
        saved = get_platform_store().upsert_membership(membership)
        get_platform_store().record_audit(_audit(principal, "membership.upserted", "membership", saved.id, account_id, space_id=saved.space_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MembershipOut(**saved.__dict__)


@router.get("/accounts/{account_id}/access-groups", response_model=list[AccessGroupOut])
def list_access_groups(
    account_id: str, space_id: str = "", principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [
        AccessGroupOut(**row.__dict__)
        for row in get_platform_store().list_access_groups(account_id, space_id)
    ]


@router.post("/accounts/{account_id}/access-groups", response_model=AccessGroupOut)
def upsert_access_group(
    account_id: str, body: AccessGroupIn, principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    try:
        saved = get_platform_store().upsert_access_group(AccessGroup(
            id=body.id or f"grp_{uuid4().hex[:16]}",
            account_id=account_id,
            name=body.name.strip(),
            kind=body.kind,
            space_id=body.space_id.strip(),
            status=body.status,
        ))
        get_platform_store().record_audit(_audit(
            principal, "access_group.upserted", "access_group", saved.id,
            account_id, space_id=saved.space_id,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AccessGroupOut(**saved.__dict__)


@router.get(
    "/accounts/{account_id}/access-group-memberships",
    response_model=list[AccessGroupMembershipOut],
)
def list_access_group_memberships(
    account_id: str, user_id: str = "", principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [
        AccessGroupMembershipOut(**row.__dict__)
        for row in get_platform_store().list_access_group_memberships(account_id, user_id)
    ]


@router.post(
    "/accounts/{account_id}/access-group-memberships",
    response_model=AccessGroupMembershipOut,
)
def upsert_access_group_membership(
    account_id: str,
    body: AccessGroupMembershipIn,
    principal: Principal = Depends(resolve_principal),
):
    authorize_account_admin(principal, account_id, get_platform_store())
    try:
        saved = get_platform_store().upsert_access_group_membership(AccessGroupMembership(
            id=body.id or f"grm_{uuid4().hex[:16]}",
            account_id=account_id,
            group_id=body.group_id.strip(),
            user_id=body.user_id.strip(),
            space_id=body.space_id.strip(),
            status=body.status,
        ))
        get_platform_store().record_audit(_audit(
            principal, "access_group_membership.upserted", "access_group_membership",
            saved.id, account_id, space_id=saved.space_id,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AccessGroupMembershipOut(**saved.__dict__)


@router.get("/accounts/{account_id}/consent", response_model=list[ConsentOut])
def list_consent(account_id: str, space_id: str = "", principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [ConsentOut(**row.__dict__) for row in get_platform_store().list_consent_records(account_id, space_id)]


@router.post("/accounts/{account_id}/consent", response_model=ConsentOut)
def upsert_consent(account_id: str, body: ConsentIn, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    record = ConsentRecord(
        id=body.id or f"cons_{uuid4().hex[:12]}",
        account_id=account_id,
        subject_ref=body.subject_ref.strip(),
        purpose=body.purpose.strip(),
        status=body.status,
        space_id=body.space_id.strip(),
        source=body.source.strip(),
        captured_by=principal.user_id,
        withdrawn_at=body.withdrawn_at.strip(),
    )
    try:
        saved = get_platform_store().upsert_consent_record(record)
        get_platform_store().record_audit(_audit(principal, "consent.upserted", "consent", saved.id, account_id, space_id=saved.space_id, purpose=saved.purpose))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConsentOut(**saved.__dict__)


@router.get("/accounts/{account_id}/retention", response_model=list[RetentionPolicyOut])
def list_retention(account_id: str, space_id: str = "", principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [RetentionPolicyOut(**row.__dict__) for row in get_platform_store().list_retention_policies(account_id, space_id)]


@router.post("/accounts/{account_id}/retention", response_model=RetentionPolicyOut)
def upsert_retention(account_id: str, body: RetentionPolicyIn, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    policy = RetentionPolicy(
        id=body.id or f"ret_{uuid4().hex[:12]}",
        account_id=account_id,
        domain=body.domain.strip(),
        record_type=body.record_type.strip(),
        action=body.action.strip(),
        duration_days=body.duration_days,
        legal_basis=body.legal_basis.strip(),
        space_id=body.space_id.strip(),
        status=body.status,
    )
    try:
        saved = get_platform_store().upsert_retention_policy(policy)
        get_platform_store().record_audit(_audit(principal, "retention_policy.upserted", "retention_policy", saved.id, account_id, space_id=saved.space_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RetentionPolicyOut(**saved.__dict__)


@router.get("/accounts/{account_id}/data-access", response_model=list[DataAccessEventOut])
def list_data_access(account_id: str, space_id: str = "", principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [DataAccessEventOut(**row.__dict__) for row in get_platform_store().list_data_access_events(account_id, space_id)]


@router.post("/accounts/{account_id}/data-access", response_model=DataAccessEventOut)
def record_data_access(account_id: str, body: DataAccessEventIn, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    event = DataAccessEvent(
        id=body.id or f"dae_{uuid4().hex[:12]}",
        account_id=account_id,
        actor_id=body.actor_id.strip(),
        actor_type=body.actor_type.strip(),
        action=body.action.strip(),
        target_type=body.target_type.strip(),
        target_id=body.target_id.strip(),
        space_id=body.space_id.strip(),
        app_id=body.app_id.strip(),
        purpose=body.purpose.strip(),
        decision=body.decision.strip(),
        meta=body.meta,
    )
    try:
        saved = get_platform_store().record_data_access(event)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DataAccessEventOut(**saved.__dict__)


@router.get("/processors", response_model=list[ProcessorOut])
def list_processors(account_id: str = "", principal: Principal = Depends(resolve_principal)):
    _require_platform_admin(principal)
    return [ProcessorOut(**row.__dict__) for row in get_platform_store().list_processors(account_id)]


@router.post("/processors", response_model=ProcessorOut)
def upsert_processor(body: ProcessorIn, principal: Principal = Depends(resolve_principal)):
    _require_platform_admin(principal)
    processor = ProcessorRegistration(id=body.id or f"proc_{uuid4().hex[:12]}", **body.model_dump(exclude={"id"}))
    try:
        saved = get_platform_store().upsert_processor(processor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProcessorOut(**saved.__dict__)


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(account_id: str = "", principal: Principal = Depends(resolve_principal)):
    _require_platform_admin(principal)
    return [ProviderOut(**row.__dict__) for row in get_platform_store().list_providers(account_id)]


@router.post("/providers", response_model=ProviderOut)
def upsert_provider(body: ProviderIn, principal: Principal = Depends(resolve_principal)):
    _require_platform_admin(principal)
    provider = ProviderRegistration(id=body.id or f"prov_{uuid4().hex[:12]}", **body.model_dump(exclude={"id"}))
    try:
        saved = get_platform_store().upsert_provider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProviderOut(**saved.__dict__)


@router.get("/accounts/{account_id}/credentials", response_model=list[CredentialMetadataOut])
def list_credentials(account_id: str, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    return [CredentialMetadataOut(**row.__dict__) for row in get_platform_store().list_credential_metadata(account_id)]


@router.post("/accounts/{account_id}/credentials", response_model=CredentialMetadataOut)
def upsert_credential(account_id: str, body: CredentialMetadataIn, principal: Principal = Depends(resolve_principal)):
    authorize_account_admin(principal, account_id, get_platform_store())
    credential = CredentialMetadata(
        id=body.id or f"cred_{uuid4().hex[:12]}",
        account_id=account_id,
        provider=body.provider.strip(),
        app_id=body.app_id.strip(),
        secret_ref=body.secret_ref.strip(),
        status=body.status,
        rotated_at=body.rotated_at.strip(),
        last_verified_at=body.last_verified_at.strip(),
        meta=body.meta,
    )
    if "secret" in str(body.meta).lower() or "password" in str(body.meta).lower():
        raise HTTPException(status_code=400, detail="Credential metadata cannot contain raw secret-like fields.")
    try:
        saved = get_platform_store().upsert_credential_metadata(credential)
        get_platform_store().record_audit(_audit(principal, "credential_metadata.upserted", "credential_metadata", saved.id, account_id, app_id=saved.app_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CredentialMetadataOut(**saved.__dict__)

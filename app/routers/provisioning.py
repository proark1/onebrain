"""Admin provisioning endpoints for modular customer rollout."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth.account_access import authorized_account_ids, is_account_admin
from app.auth.principal import Principal, resolve_principal
from app.config import get_settings
from app.deps import (
    get_control_plane_store,
    get_fleet_store,
    get_platform_store,
    get_provisioning_run_store,
    get_service_key_store,
    get_user_store,
)
from app.platform.base import BrandTheme, DEFAULT_BRAND_THEME
from app.provisioning.bundles import BUNDLES, ProvisioningBundle
from app.provisioning.hetzner.broker import build_hetzner_broker
from app.provisioning.hetzner.provisioner import HetznerProvisioner
from app.provisioning.runs import (
    ProvisioningCallback,
    ProvisioningRun,
    STATUS_CANCELLED,
    STATUS_DISPATCH_FAILED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    apply_callback,
    create_run,
    mark_dispatch_failed,
    read_one_time_secret,
    verify_callback_secret,
)
from app.provisioning.service import CustomerProvisioner, ProvisioningResult, normalize_id
from app.schemas import BrandThemeOut

router = APIRouter(prefix="/api/provisioning", tags=["provisioning"])

# Structural provisioning inputs (versions, slugs, module ids, hex colors) are
# passed to the infrastructure renderer and signed release machinery. Constrain
# them at the trust boundary; customer and brand names remain free text because
# they are persisted data rather than shell or cloud-init identifiers.
_STRUCTURAL_SAFE = re.compile(r"^[A-Za-z0-9._:/+#-]*$")


def _reject_unsafe(value: str, field: str) -> str:
    if value and not _STRUCTURAL_SAFE.match(value):
        raise ValueError(
            f"{field} may only contain letters, digits, and . _ : / + # - "
            "(no quotes, whitespace, or shell metacharacters)."
        )
    return value


class BundleOut(BaseModel):
    id: str
    label: str
    description: str
    spaces: list[str]
    apps: list[str]
    modules: list[str]


class BrandThemeInput(BaseModel):
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

    @field_validator(
        "primary_color", "secondary_color", "accent_color", "background_color",
        "surface_color", "text_color", "muted_color", "success_color",
        "warning_color", "danger_color",
    )
    @classmethod
    def _colors_are_inert(cls, v: str) -> str:
        return _reject_unsafe(v, "color")


class CustomerProvisionCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    bundle_id: str = Field(default="full_stack", max_length=80)
    account_kind: str = Field(default="organization", pattern="^(person|organization|family|project)$")
    account_id: str | None = Field(default=None, max_length=120)
    deployment_id: str | None = Field(default=None, max_length=120)
    # The owner admin's email. When set (and a user store is wired), CustomerProvisioner
    # mints the owner with a one-time password; that OTP is ONEBRAIN_ADMIN_PASSWORD, a
    # REQUIRED Hetzner bundle key — without it a hetzner-backend provision fails validate_bundle
    # and dispatch_fails. Free-text (an email carries @/./+, so it is NOT charset-constrained
    # like structural identifiers); the OTP, not the email, reaches the box.
    owner_email: str = Field(default="", max_length=320)
    deployment_type: str = Field(
        default="dedicated_server",
        max_length=80,
        pattern="^(dedicated_server|customer_owned)$",
    )
    environment: str = Field(default="production", max_length=80)
    region: str = Field(default="", max_length=80)
    release_ring: str = Field(default="manual", max_length=80)
    initial_version: str = Field(default="0.1.0", min_length=1, max_length=80)
    current_migration: str = Field(default="", max_length=80)
    module_versions: dict[str, str] = Field(default_factory=dict)
    mint_integration_keys: bool = True
    brand_theme: BrandThemeInput | None = None
    app_brand_themes: dict[str, BrandThemeInput] = Field(default_factory=dict)
    external_provisioning: bool = False
    dry_run: bool = True
    callback_url: str = Field(default="", max_length=500)

    @field_validator(
        "bundle_id", "deployment_type", "environment", "region", "release_ring",
        "initial_version", "current_migration",
    )
    @classmethod
    def _structural_fields_are_inert(cls, v: str) -> str:
        return _reject_unsafe(v, "field")

    @field_validator("module_versions")
    @classmethod
    def _module_versions_are_inert(cls, v: dict[str, str]) -> dict[str, str]:
        for key, val in v.items():
            _reject_unsafe(key, "module id")
            _reject_unsafe(val, "module version")
        return v


class ProvisioningCallbackIn(BaseModel):
    # A box only reports its own status; it never selects or overwrites the
    # provisioned target. Reject retired provider-coordinate fields explicitly.
    model_config = ConfigDict(extra="forbid")

    status: str = Field(max_length=40)
    external_run_id: str = Field(default="", max_length=200)
    external_run_url: str = Field(default="", max_length=500)
    result_payload: dict = Field(default_factory=dict)
    service_urls: dict[str, str] = Field(default_factory=dict)
    migration_revision: str = Field(default="", max_length=120)
    smoke_status: str = Field(default="", max_length=80)
    failure_reason: str = Field(default="", max_length=1000)
    bootstrap_password: str = Field(default="", max_length=500)


class ProvisionedAccountOut(BaseModel):
    id: str
    kind: str
    name: str
    owner_user_id: str = ""


class ProvisionedSpaceOut(BaseModel):
    id: str
    kind: str
    name: str


class ProvisionedAppOut(BaseModel):
    id: str
    app_id: str
    enabled_space_ids: list[str]
    allowed_purposes: list[str]
    display_name: str = ""


class ProvisionedDeploymentOut(BaseModel):
    id: str
    customer_name: str
    deployment_type: str
    region: str = ""
    release_ring: str
    current_version: str
    current_migration: str = ""


class ProvisionedModuleOut(BaseModel):
    module_id: str
    version: str
    status: str


class ProvisionedCredentialOut(BaseModel):
    id: str
    key: str
    tenant_id: str
    account_id: str
    app_id: str
    label: str
    scopes: list[str]
    space_ids: list[str]
    purposes: list[str]


class ProvisioningResultOut(BaseModel):
    bundle_id: str
    account: ProvisionedAccountOut
    spaces: list[ProvisionedSpaceOut]
    apps: list[ProvisionedAppOut]
    deployment: ProvisionedDeploymentOut
    modules: list[ProvisionedModuleOut]
    credentials: list[ProvisionedCredentialOut] = Field(default_factory=list)
    brand_theme: BrandThemeOut
    app_brand_themes: list[BrandThemeOut] = Field(default_factory=list)
    provisioning_run: "ProvisioningRunOut | None" = None


class ProvisioningRunOut(BaseModel):
    id: str
    account_id: str
    deployment_id: str
    bundle_id: str
    requested_by: str
    status: str
    external_provider: str = ""
    external_run_id: str = ""
    external_run_url: str = ""
    # The persistent store retains its legacy column names during the database
    # migration window.  The public API is provider-neutral: only a Hetzner
    # provisioning dispatch may establish these target coordinates.
    target_id: str = ""
    target_environment: str = ""
    service_urls: dict[str, str] = Field(default_factory=dict)
    migration_revision: str = ""
    smoke_status: str = ""
    failure_reason: str = ""
    result_payload: dict = Field(default_factory=dict)
    bootstrap_secret_id: str = ""
    retry_of_run_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    dispatched_at: str = ""
    completed_at: str = ""


class BootstrapSecretOut(BaseModel):
    secret_id: str
    plaintext: str


def _authorize_run_account(principal: Principal, account_id: str) -> None:
    """Unless on Mission Control, require the caller administer the run's account
    (same-404 as elsewhere so run existence / ids can't be probed)."""
    if get_settings().operator_mode:
        return
    platform = get_platform_store()
    if not is_account_admin(principal, platform.get_account(account_id), platform):
        raise HTTPException(status_code=404, detail="Provisioning run not found.")


def _require_admin(principal: Principal) -> None:
    # Defense in depth: provisioning is assembly-gated on is_operator_surface
    # (app/main.py). Refuse at request time too so a mis-wired customer stack can
    # never dispatch deployments, run callbacks, or read bootstrap secrets.
    if not get_settings().is_operator_surface:
        raise HTTPException(status_code=404, detail="Not found.")
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can provision customers.")


def _validate_callback_url(url: str, *, placeholder: str = "{run_id}") -> None:
    """Validate a token-bearing callback URL and its required ID placeholder.

    Reject attacker-controlled destinations that could exfiltrate that token.
    Require HTTPS and, when configured, an allowlisted host. Provisioning runs
    use ``{run_id}``; rollout delivery uses ``{rollout_id}``.
    """
    from urllib.parse import urlsplit

    cleaned = url.strip()
    # Defense in depth alongside cloud-init rendering: reject shell
    # metacharacters that enable command substitution or quote breakout
    # (path/query included). '&', '?', '=' are intentionally allowed so a
    # legitimate multi-parameter query string still passes, as does the ID
    # placeholder braces (harmless with '$' already rejected).
    if any(c in cleaned for c in "$`()|;<>\\'\" \t\n\r"):
        raise HTTPException(status_code=400, detail="callback_url contains invalid characters.")
    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="callback_url must be a valid absolute https URL.") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise HTTPException(status_code=400, detail="callback_url must be an absolute https URL.")
    # The run/rollout does not exist until after this preflight. Requiring its
    # literal template prevents a box from posting a bearer token to an
    # ambiguous or caller-selected identifier once it boots.
    if placeholder not in cleaned:
        raise HTTPException(status_code=400, detail=f"callback_url must contain the {placeholder} placeholder.")
    allowed = [h.strip().lower() for h in get_settings().provisioning_callback_allowed_hosts.split(",") if h.strip()]
    if allowed and parts.hostname.lower() not in allowed:
        raise HTTPException(status_code=400, detail="callback_url host is not allowed.")


def _bundle_out(bundle: ProvisioningBundle) -> BundleOut:
    return BundleOut(
        id=bundle.id,
        label=bundle.label,
        description=bundle.description,
        spaces=[space.kind for space in bundle.spaces],
        apps=[app.app_id for app in bundle.apps],
        modules=list(bundle.modules),
    )


def _theme_from_input(account_id: str, body: BrandThemeInput | None, app_id: str = "") -> BrandTheme | None:
    if body is None:
        return None
    return BrandTheme(
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
        source="provisioning",
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


def _default_account_id(customer_name: str) -> str:
    try:
        stem = normalize_id(customer_name)[:40]
    except ValueError:
        stem = "customer"
    return f"acct_{stem}_{uuid4().hex[:6]}"


def _run_out(run: ProvisioningRun) -> ProvisioningRunOut:
    return ProvisioningRunOut(
        id=run.id,
        account_id=run.account_id,
        deployment_id=run.deployment_id,
        bundle_id=run.bundle_id,
        requested_by=run.requested_by,
        status=run.status,
        external_provider=run.external_provider,
        external_run_id=run.external_run_id,
        external_run_url=run.external_run_url,
        target_id=run.railway_project_id,
        target_environment=run.railway_environment_id,
        service_urls=run.service_urls,
        migration_revision=run.migration_revision,
        smoke_status=run.smoke_status,
        failure_reason=run.failure_reason,
        result_payload=run.result_payload,
        bootstrap_secret_id=run.bootstrap_secret_id,
        retry_of_run_id=run.retry_of_run_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
        dispatched_at=run.dispatched_at,
        completed_at=run.completed_at,
    )


def _box_integration_credentials(result: ProvisioningResult) -> dict[str, tuple[str, str]]:
    """Return app-addressed credentials for services installed on the customer box."""
    return {
        cred.app_id: (cred.key, cred.space_ids[0] if cred.space_ids else "")
        for cred in result.credentials
        if cred.app_id in {"assistant", "communication"} and cred.key
    }


def _dispatch_run(run: ProvisioningRun, *, owner_otp: str = "",
                  service_key: str = "", space_id: str = "", owner_email: str = "",
                  integration_credentials: dict[str, tuple[str, str]] | None = None) -> ProvisioningRun:
    # H-1/H-9: Hetzner is the only supported external provisioning backend. A
    # non-Hetzner setting fails closed; it never falls back to a secondary provider.
    # owner_otp/service_key/space_id/owner_email (G3-3) are threaded from
    # provision_customer into the box secret bundle. owner_email + owner_otp are
    # the ONEBRAIN_ADMIN_EMAIL/PASSWORD pair the box needs to seed a loginable admin.
    # A retry re-dispatch passes none; the Hetzner path then reuses the stored bundle.
    store = get_provisioning_run_store()
    settings = get_settings()
    # getattr keeps pre-P4 settings fakes fail-closed until their test fixture
    # explicitly selects the supported backend.
    backend = getattr(settings, "provisioner_backend", "disabled")
    try:
        if backend != "hetzner":
            return mark_dispatch_failed(store, run, "Hetzner provisioning is required.")
        dispatched = HetznerProvisioner(
            settings, build_hetzner_broker(settings), get_control_plane_store(),
            prov_store=store, fleet_store=get_fleet_store(),
        ).dispatch(run, owner_otp=owner_otp, service_key=service_key, space_id=space_id,
                   owner_email=owner_email, integration_credentials=integration_credentials)
    except (RuntimeError, OSError) as exc:
        return mark_dispatch_failed(store, run, str(exc))
    return store.update_run(dispatched)


def _result_out(result: ProvisioningResult, run: ProvisioningRun | None = None) -> ProvisioningResultOut:
    deployment = result.deployment
    return ProvisioningResultOut(
        bundle_id=result.bundle.id,
        account=ProvisionedAccountOut(
            id=result.account.id,
            kind=result.account.kind,
            name=result.account.name,
            owner_user_id=result.account.owner_user_id,
        ),
        spaces=[ProvisionedSpaceOut(id=s.id, kind=s.kind, name=s.name) for s in result.spaces],
        apps=[
            ProvisionedAppOut(
                id=app.id,
                app_id=app.app_id,
                enabled_space_ids=list(app.enabled_space_ids),
                allowed_purposes=list(app.allowed_purposes),
                display_name=app.display_name,
            )
            for app in result.installations
        ],
        deployment=ProvisionedDeploymentOut(
            id=deployment.id,
            customer_name=deployment.customer_name,
            deployment_type=deployment.deployment_type,
            region=deployment.region,
            release_ring=deployment.release_ring,
            current_version=deployment.current_version,
            current_migration=deployment.current_migration,
        ),
        modules=[
            ProvisionedModuleOut(module_id=m.module_id, version=m.version, status=m.status)
            for m in result.modules
        ],
        brand_theme=_brand_theme_out(result.brand_theme),
        app_brand_themes=[_brand_theme_out(theme) for theme in result.app_brand_themes],
        credentials=[
            ProvisionedCredentialOut(
                id=c.id,
                key=c.key,
                tenant_id=c.tenant_id,
                account_id=c.account_id,
                app_id=c.app_id,
                label=c.label,
                scopes=c.scopes,
                space_ids=c.space_ids,
                purposes=c.purposes,
            )
            for c in result.credentials
        ],
        provisioning_run=_run_out(run) if run else None,
    )


@router.get("/bundles", response_model=list[BundleOut])
def list_bundles(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_bundle_out(bundle) for bundle in BUNDLES.values()]


@router.post("/customers", response_model=ProvisioningResultOut)
def provision_customer(body: CustomerProvisionCreate, principal: Principal = Depends(resolve_principal)):
    return _provision_customer_impl(body, principal)


def _provision_customer_impl(body: CustomerProvisionCreate, principal: Principal):
    _require_admin(principal)
    if body.external_provisioning:
        settings = get_settings()
        # Check the entire production Mission Control contract before creating
        # platform rows or a provisioning-run record. A configuration error must
        # not leave a half-created customer that no secure executor can serve.
        preflight = getattr(settings, "assert_production_mission_control_ready", None)
        if callable(preflight):
            try:
                preflight()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not body.callback_url.strip():
            raise HTTPException(status_code=400, detail="External provisioning requires a callback URL.")
        _validate_callback_url(body.callback_url)
        # A Hetzner box cannot come up without the owner OTP (ONEBRAIN_ADMIN_PASSWORD is a
        # REQUIRED bundle key). Fail FAST with a clear reason rather than letting the box
        # secret bundle fail validate_bundle later and surface as an opaque dispatch_failed.
        if getattr(settings, "provisioner_backend", "disabled") == "hetzner" and not body.owner_email.strip():
            raise HTTPException(
                status_code=400,
                detail="A Hetzner provision requires owner_email: the owner one-time password "
                       "is the required ONEBRAIN_ADMIN_PASSWORD secret-bundle key.")
    account_id = body.account_id or _default_account_id(body.customer_name)
    deployment_id = body.deployment_id or f"dep_{account_id}"
    try:
        result = CustomerProvisioner(
            get_platform_store(), get_control_plane_store(), get_service_key_store(),
            get_user_store(),
        ).provision(
            account_id=account_id,
            account_kind=body.account_kind,
            customer_name=body.customer_name,
            owner_user_id=principal.user_id,
            owner_email=body.owner_email,
            bundle_id=body.bundle_id,
            deployment_id=deployment_id,
            deployment_type=body.deployment_type,
            environment=body.environment,
            region=body.region,
            release_ring=body.release_ring,
            initial_version=body.initial_version,
            current_migration=body.current_migration,
            module_versions=body.module_versions,
            mint_integration_keys=body.mint_integration_keys,
            brand_theme=_theme_from_input(account_id, body.brand_theme),
            app_brand_themes={
                app_id: theme
                for app_id, theme in (
                    (key.strip(), _theme_from_input(account_id, value, key.strip()))
                    for key, value in body.app_brand_themes.items()
                )
                if app_id and theme
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run = None
    if body.external_provisioning:
        payload = {
            "customer_name": body.customer_name,
            "account_kind": body.account_kind,
            "deployment_type": body.deployment_type,
            "region": body.region,
            "release_ring": body.release_ring,
            "initial_version": body.initial_version,
            "current_migration": body.current_migration,
            "module_versions": body.module_versions,
            "brand_theme": body.brand_theme.model_dump() if body.brand_theme else {},
            "callback_url": body.callback_url,
            "dry_run": body.dry_run,
        }
        run = create_run(
            get_provisioning_run_store(),
            account_id=result.account.id,
            deployment_id=result.deployment.id,
            bundle_id=result.bundle.id,
            requested_by=principal.user_id,
            payload=payload,
        )
        # G3-3: the box secret bundle needs the owner OTP + a comm/assistant integration
        # service key + its space id — all minted by CustomerProvisioner above, NOT
        # visible inside HetznerProvisioner.dispatch. Thread them from the result here
        # (the seam where BOTH the run and the provision result are in scope). Empty for
        # a box with no owner/integration module.
        integration_credentials = _box_integration_credentials(result)
        run = _dispatch_run(run, owner_otp=result.owner_one_time_password,
                            integration_credentials=integration_credentials,
                            owner_email=body.owner_email)
    return _result_out(result, run)


@router.get("/runs", response_model=list[ProvisioningRunOut])
def list_provisioning_runs(
    account_id: str = "",
    deployment_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    runs = get_provisioning_run_store().list_runs(account_id, deployment_id)
    if not get_settings().operator_mode:
        allowed = authorized_account_ids(principal, get_platform_store())
        runs = [run for run in runs if run.account_id in allowed]
    return [_run_out(run) for run in runs]


@router.get("/runs/{run_id}", response_model=ProvisioningRunOut)
def get_provisioning_run(run_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    run = get_provisioning_run_store().get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Provisioning run not found.")
    _authorize_run_account(principal, run.account_id)
    return _run_out(run)


@router.post("/runs/{run_id}/retry", response_model=ProvisioningRunOut)
def retry_provisioning_run(run_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_provisioning_run_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Provisioning run not found.")
    _authorize_run_account(principal, run.account_id)
    if run.status not in {STATUS_FAILED, STATUS_CANCELLED, STATUS_DISPATCH_FAILED}:
        raise HTTPException(status_code=409, detail="Only failed, cancelled, or dispatch-failed runs can be retried.")
    retry = create_run(
        store,
        account_id=run.account_id,
        deployment_id=run.deployment_id,
        bundle_id=run.bundle_id,
        requested_by=principal.user_id,
        payload=run.request_payload,
        retry_of_run_id=run.id,
    )
    return _run_out(_dispatch_run(retry))


def _require_run_callback_auth(run, authorization: str) -> None:
    """Authenticate only the callback token minted for this Hetzner run.

    A missing run or missing per-run token returns the same 401 response, so the
    callback endpoint does not reveal whether a run ID exists.
    """
    prefix = "Bearer "
    per_run_hash = (run.result_payload or {}).get("callback_token_hash", "") if run is not None else ""
    if not per_run_hash or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Invalid provisioning callback token.")
    token = authorization[len(prefix):].strip()
    if not verify_callback_secret(token, per_run_hash):
        raise HTTPException(status_code=401, detail="Invalid provisioning callback token.")


@router.post("/runs/{run_id}/callback", response_model=ProvisioningRunOut)
def provisioning_callback(
    run_id: str,
    body: ProvisioningCallbackIn,
    authorization: str = Header(default=""),
):
    store = get_provisioning_run_store()
    # Load the run first so its token hash is the only accepted callback credential.
    _require_run_callback_auth(store.get_run(run_id), authorization)
    try:
        run = apply_callback(
            store,
            get_settings(),
            run_id,
            ProvisioningCallback(**body.model_dump()),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Provisioning run not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run.status == STATUS_SUCCEEDED and not bool((run.request_payload or {}).get("dry_run")):
        get_control_plane_store().mark_deployment_provisioned(
            run.deployment_id,
            installed_at=run.completed_at,
            version=str((run.request_payload or {}).get("initial_version", "")),
            migration=run.migration_revision,
        )
    return _run_out(run)


@router.post("/runs/{run_id}/bootstrap-secret/read", response_model=BootstrapSecretOut)
def read_bootstrap_secret(run_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    run = get_provisioning_run_store().get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Provisioning run not found.")
    # Off Mission Control, an admin may only read a secret for an account they
    # administer (same-404 so run existence can't be probed). The operator on
    # Mission Control legitimately reads any provisioned customer's bootstrap.
    if not get_settings().operator_mode:
        platform = get_platform_store()
        if not is_account_admin(principal, platform.get_account(run.account_id), platform):
            raise HTTPException(status_code=404, detail="Provisioning run not found.")
    if not run.bootstrap_secret_id:
        raise HTTPException(status_code=404, detail="Bootstrap secret not available.")
    try:
        plaintext = read_one_time_secret(get_provisioning_run_store(), get_settings(), run.bootstrap_secret_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Bootstrap secret not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return BootstrapSecretOut(secret_id=run.bootstrap_secret_id, plaintext=plaintext)

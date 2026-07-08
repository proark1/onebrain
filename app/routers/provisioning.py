"""Admin provisioning endpoints for modular customer rollout."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.principal import Principal, resolve_principal
from app.deps import get_control_plane_store, get_platform_store, get_service_key_store
from app.provisioning.bundles import BUNDLES, ProvisioningBundle
from app.provisioning.service import CustomerProvisioner, ProvisioningResult, normalize_id

router = APIRouter(prefix="/api/provisioning", tags=["provisioning"])


class BundleOut(BaseModel):
    id: str
    label: str
    description: str
    spaces: list[str]
    apps: list[str]
    modules: list[str]


class CustomerProvisionCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    bundle_id: str = Field(default="full_stack", max_length=80)
    account_kind: str = Field(default="organization", pattern="^(person|organization|family|project)$")
    account_id: str | None = Field(default=None, max_length=120)
    deployment_id: str | None = Field(default=None, max_length=120)
    deployment_type: str = Field(default="dedicated_railway", max_length=80)
    region: str = Field(default="", max_length=80)
    release_ring: str = Field(default="manual", max_length=80)
    initial_version: str = Field(default="0.1.0", min_length=1, max_length=80)
    current_migration: str = Field(default="", max_length=80)
    module_versions: dict[str, str] = Field(default_factory=dict)
    mint_integration_keys: bool = True


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


def _require_admin(principal: Principal) -> None:
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can provision customers.")


def _bundle_out(bundle: ProvisioningBundle) -> BundleOut:
    return BundleOut(
        id=bundle.id,
        label=bundle.label,
        description=bundle.description,
        spaces=[space.kind for space in bundle.spaces],
        apps=[app.app_id for app in bundle.apps],
        modules=list(bundle.modules),
    )


def _default_account_id(customer_name: str) -> str:
    try:
        stem = normalize_id(customer_name)[:40]
    except ValueError:
        stem = "customer"
    return f"acct_{stem}_{uuid4().hex[:6]}"


def _result_out(result: ProvisioningResult) -> ProvisioningResultOut:
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
    )


@router.get("/bundles", response_model=list[BundleOut])
def list_bundles(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_bundle_out(bundle) for bundle in BUNDLES.values()]


@router.post("/customers", response_model=ProvisioningResultOut)
def provision_customer(body: CustomerProvisionCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    account_id = body.account_id or _default_account_id(body.customer_name)
    deployment_id = body.deployment_id or f"dep_{account_id}"
    try:
        result = CustomerProvisioner(
            get_platform_store(), get_control_plane_store(), get_service_key_store(),
        ).provision(
            account_id=account_id,
            account_kind=body.account_kind,
            customer_name=body.customer_name,
            owner_user_id=principal.user_id,
            bundle_id=body.bundle_id,
            deployment_id=deployment_id,
            deployment_type=body.deployment_type,
            region=body.region,
            release_ring=body.release_ring,
            initial_version=body.initial_version,
            current_migration=body.current_migration,
            module_versions=body.module_versions,
            mint_integration_keys=body.mint_integration_keys,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _result_out(result)

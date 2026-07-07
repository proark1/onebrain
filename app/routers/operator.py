"""Operator control-plane endpoints.

These endpoints track deployment metadata and release state only. They do not
expose customer content.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.principal import Principal, resolve_principal
from app.controlplane.base import (
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    RolloutRun,
    UpdatePlan,
)
from app.deps import get_control_plane_store

router = APIRouter(prefix="/api/operator", tags=["operator"])


class DeploymentCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    environment: str = Field(default="production", max_length=80)
    deployment_type: str = Field(default="dedicated_railway", max_length=80)
    region: str = Field(default="", max_length=80)
    release_ring: str = Field(default="manual", max_length=80)
    status: str = Field(default="active", max_length=80)
    current_version: str = Field(default="", max_length=80)
    current_migration: str = Field(default="", max_length=80)
    id: str | None = Field(default=None, max_length=120)


class DeploymentOut(BaseModel):
    id: str
    customer_name: str
    environment: str
    deployment_type: str
    region: str
    release_ring: str
    status: str
    current_version: str = ""
    current_migration: str = ""


class ModuleUpsert(BaseModel):
    module_id: str
    version: str
    status: str = "active"


class ModuleOut(BaseModel):
    deployment_id: str
    module_id: str
    version: str
    status: str


class ReleaseCreate(BaseModel):
    version: str
    git_sha: str
    modules: dict[str, str]
    migration_from: str = ""
    migration_to: str = ""
    security_notes: str = ""
    rollback_plan: str = ""
    status: str = "draft"


class ReleaseOut(BaseModel):
    version: str
    git_sha: str
    modules: dict[str, str]
    migration_from: str = ""
    migration_to: str = ""
    security_notes: str = ""
    rollback_plan: str = ""
    status: str = "draft"


class BackupCreate(BaseModel):
    status: str
    detail: str = ""
    id: str | None = None


class BackupOut(BaseModel):
    id: str
    deployment_id: str
    status: str
    detail: str = ""


class HealthCreate(BaseModel):
    status: str
    detail: str = ""
    id: str | None = None


class HealthOut(BaseModel):
    id: str
    deployment_id: str
    status: str
    detail: str = ""


class UpdatePlanOut(BaseModel):
    deployment_id: str
    target_version: str
    allowed: bool
    reason: str
    current_modules: dict[str, str] = Field(default_factory=dict)
    target_modules: dict[str, str] = Field(default_factory=dict)
    modules_to_update: dict[str, str] = Field(default_factory=dict)


class RolloutCreate(BaseModel):
    target_version: str
    status: str = "pending"
    notes: str = ""
    id: str | None = None


class RolloutOut(BaseModel):
    id: str
    deployment_id: str
    target_version: str
    status: str
    started_by: str
    notes: str = ""


def _require_admin(principal: Principal) -> None:
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can manage operator deployments.")


def _deployment_out(d: CustomerDeployment) -> DeploymentOut:
    return DeploymentOut(**{k: getattr(d, k) for k in DeploymentOut.model_fields})


def _module_out(m: DeploymentModule) -> ModuleOut:
    return ModuleOut(deployment_id=m.deployment_id, module_id=m.module_id, version=m.version, status=m.status)


def _release_out(r: ReleaseManifest) -> ReleaseOut:
    return ReleaseOut(
        version=r.version, git_sha=r.git_sha, modules=r.modules, migration_from=r.migration_from,
        migration_to=r.migration_to, security_notes=r.security_notes, rollback_plan=r.rollback_plan,
        status=r.status,
    )


def _backup_out(b: BackupRun) -> BackupOut:
    return BackupOut(id=b.id, deployment_id=b.deployment_id, status=b.status, detail=b.detail)


def _health_out(h: HealthCheckRun) -> HealthOut:
    return HealthOut(id=h.id, deployment_id=h.deployment_id, status=h.status, detail=h.detail)


def _plan_out(p: UpdatePlan) -> UpdatePlanOut:
    return UpdatePlanOut(
        deployment_id=p.deployment_id, target_version=p.target_version, allowed=p.allowed,
        reason=p.reason, current_modules=p.current_modules, target_modules=p.target_modules,
        modules_to_update=p.modules_to_update,
    )


def _rollout_out(r: RolloutRun) -> RolloutOut:
    return RolloutOut(
        id=r.id, deployment_id=r.deployment_id, target_version=r.target_version,
        status=r.status, started_by=r.started_by, notes=r.notes,
    )


@router.get("/deployments", response_model=list[DeploymentOut])
def list_deployments(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_deployment_out(d) for d in get_control_plane_store().list_deployments()]


@router.post("/deployments", response_model=DeploymentOut)
def create_deployment(body: DeploymentCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        deployment = get_control_plane_store().create_deployment(CustomerDeployment(
            id=body.id or f"dep_{uuid4().hex[:12]}",
            customer_name=body.customer_name.strip(),
            environment=body.environment.strip(),
            deployment_type=body.deployment_type.strip(),
            region=body.region.strip(),
            release_ring=body.release_ring.strip(),
            status=body.status.strip(),
            current_version=body.current_version.strip(),
            current_migration=body.current_migration.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _deployment_out(deployment)


@router.get("/deployments/{deployment_id}/modules", response_model=list[ModuleOut])
def list_modules(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_module_out(m) for m in get_control_plane_store().list_modules(deployment_id)]


@router.post("/deployments/{deployment_id}/modules", response_model=ModuleOut)
def upsert_module(deployment_id: str, body: ModuleUpsert, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        module = get_control_plane_store().upsert_module(DeploymentModule(
            deployment_id=deployment_id,
            module_id=body.module_id.strip(),
            version=body.version.strip(),
            status=body.status.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _module_out(module)


@router.get("/releases", response_model=list[ReleaseOut])
def list_releases(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_release_out(r) for r in get_control_plane_store().list_releases()]


@router.post("/releases", response_model=ReleaseOut)
def create_release(body: ReleaseCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        release = get_control_plane_store().create_release(ReleaseManifest(
            version=body.version.strip(),
            git_sha=body.git_sha.strip(),
            modules={k.strip(): v.strip() for k, v in body.modules.items()},
            migration_from=body.migration_from.strip(),
            migration_to=body.migration_to.strip(),
            security_notes=body.security_notes.strip(),
            rollback_plan=body.rollback_plan.strip(),
            status=body.status.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _release_out(release)


@router.post("/deployments/{deployment_id}/backups", response_model=BackupOut)
def record_backup(deployment_id: str, body: BackupCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        backup = get_control_plane_store().record_backup(BackupRun(
            id=body.id or f"bak_{uuid4().hex[:12]}",
            deployment_id=deployment_id,
            status=body.status.strip(),
            detail=body.detail.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _backup_out(backup)


@router.post("/deployments/{deployment_id}/health", response_model=HealthOut)
def record_health(deployment_id: str, body: HealthCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        health = get_control_plane_store().record_health(HealthCheckRun(
            id=body.id or f"hlth_{uuid4().hex[:12]}",
            deployment_id=deployment_id,
            status=body.status.strip(),
            detail=body.detail.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _health_out(health)


@router.get("/deployments/{deployment_id}/update-plan/{target_version}", response_model=UpdatePlanOut)
def update_plan(deployment_id: str, target_version: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return _plan_out(get_control_plane_store().plan_update(deployment_id, target_version))


@router.get("/deployments/{deployment_id}/rollouts", response_model=list[RolloutOut])
def list_rollouts(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_rollout_out(r) for r in get_control_plane_store().list_rollouts(deployment_id)]


@router.post("/deployments/{deployment_id}/rollouts", response_model=RolloutOut)
def start_rollout(deployment_id: str, body: RolloutCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        rollout = get_control_plane_store().start_rollout(RolloutRun(
            id=body.id or f"roll_{uuid4().hex[:12]}",
            deployment_id=deployment_id,
            target_version=body.target_version.strip(),
            status=body.status.strip(),
            started_by=principal.user_id,
            notes=body.notes.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _rollout_out(rollout)

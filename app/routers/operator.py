"""Operator control-plane endpoints.

These endpoints track deployment metadata and release state only. They do not
expose customer content.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from app.config import get_settings
from app.deps import (
    get_control_plane_store,
    get_intake_store,
    get_job_store,
    get_platform_store,
    get_service_key_store,
    get_store,
)
from app.monitoring import MonitoringSummary, monitoring_snapshot
from app.platform.base import AuditEvent
from app.schemas import BrandThemeOut, ServiceKeyInfo

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


class RolloutStatusUpdate(BaseModel):
    status: str
    notes: str = ""


class RolloutOut(BaseModel):
    id: str
    deployment_id: str
    target_version: str
    status: str
    started_by: str
    notes: str = ""


class OperatorAccountOut(BaseModel):
    id: str
    kind: str
    name: str
    owner_user_id: str = ""
    status: str = "active"


class OperatorSpaceOut(BaseModel):
    id: str
    kind: str
    name: str
    status: str = "active"


class OperatorAppOut(BaseModel):
    id: str
    app_id: str
    display_name: str = ""
    enabled_space_ids: list[str] = Field(default_factory=list)
    allowed_purposes: list[str] = Field(default_factory=list)
    status: str = "active"


class CustomerOverviewOut(BaseModel):
    account: OperatorAccountOut
    spaces: list[OperatorSpaceOut] = Field(default_factory=list)
    apps: list[OperatorAppOut] = Field(default_factory=list)
    brand_theme: BrandThemeOut
    brand_themes: list[BrandThemeOut] = Field(default_factory=list)
    service_keys: list[ServiceKeyInfo] = Field(default_factory=list)
    deployment: DeploymentOut | None = None
    modules: list[ModuleOut] = Field(default_factory=list)
    backup: BackupOut | None = None
    health: HealthOut | None = None
    latest_rollout: RolloutOut | None = None
    readiness: str = "not_deployed"


class OperatorRuntimeOut(BaseModel):
    vector_store: str
    llm_provider: str
    embeddings_provider: str
    async_ingestion: bool


class OperatorRetrievalOut(BaseModel):
    top_k: int
    min_score: float


class OperatorStorageOut(BaseModel):
    chunks: int
    intake_records: int


class OperatorServiceKeysOut(BaseModel):
    total: int
    active: int
    revoked: int


class OperatorJobFailureOut(BaseModel):
    id: str
    type: str
    tenant_id: str
    account_id: str = ""
    space_id: str = ""
    attempts: int = 0
    max_attempts: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class OperatorJobsOut(BaseModel):
    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    recent_failures: list[OperatorJobFailureOut] = Field(default_factory=list)


class OperatorSecurityOut(BaseModel):
    environment: str
    production_like: bool
    pgvector_required: bool
    database_url_configured: bool
    rls_enforced: bool
    cookie_secure: bool
    pii_phase: str


class OperatorWorkerOut(BaseModel):
    expected: bool
    pending_jobs: int
    running_jobs: int
    failed_jobs: int
    status: str


class OperatorAuthOut(BaseModel):
    total_failures: int
    login_failures: int
    service_key_failures: int
    lockouts: int
    last_failure_at: str = ""


class OperatorApiOut(BaseModel):
    errors_5xx: int
    last_error_at: str = ""
    last_error_route: str = ""
    last_error_status: int = 0


class OperatorAlertOut(BaseModel):
    id: str
    severity: str
    title: str
    detail: str
    action: str
    signal: str


class OperatorObservabilityOut(BaseModel):
    generated_at: str
    runtime: OperatorRuntimeOut
    retrieval: OperatorRetrievalOut
    storage: OperatorStorageOut
    service_keys: OperatorServiceKeysOut
    jobs: OperatorJobsOut
    security: OperatorSecurityOut
    worker: OperatorWorkerOut
    auth: OperatorAuthOut
    api: OperatorApiOut
    alerts: list[OperatorAlertOut] = Field(default_factory=list)


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


def _account_out(account) -> OperatorAccountOut:
    return OperatorAccountOut(
        id=account.id,
        kind=account.kind,
        name=account.name,
        owner_user_id=account.owner_user_id,
        status=account.status,
    )


def _space_out(space) -> OperatorSpaceOut:
    return OperatorSpaceOut(id=space.id, kind=space.kind, name=space.name, status=space.status)


def _app_out(app) -> OperatorAppOut:
    return OperatorAppOut(
        id=app.id,
        app_id=app.app_id,
        display_name=app.display_name,
        enabled_space_ids=list(app.enabled_space_ids),
        allowed_purposes=list(app.allowed_purposes),
        status=app.status,
    )


def _service_key_out(k) -> ServiceKeyInfo:
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


def _record_operator_key_audit(action: str, principal: Principal, key) -> None:
    get_platform_store().record_audit(AuditEvent(
        id=f"aud_{uuid4().hex}",
        account_id=key.account_id or key.tenant_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type="service_key",
        target_id=key.id,
        space_id=key.space_ids[0] if len(key.space_ids) == 1 else "",
        app_id=key.app_id,
        purpose=key.purposes[0] if len(key.purposes) == 1 else "",
        decision="recorded",
        meta={
            "label": key.label,
            "account_id": key.account_id,
            "app_id": key.app_id,
            "space_ids": list(key.space_ids),
            "purposes": list(key.purposes),
        },
    ))


def _require_account(account_id: str) -> None:
    if not get_platform_store().get_account(account_id):
        raise HTTPException(status_code=404, detail="Account not found.")


def _deployment_for_account(account, deployments, platform_store):
    by_id = {deployment.id: deployment for deployment in deployments}
    default_id = f"dep_{account.id}"
    if default_id in by_id:
        return by_id[default_id]

    for event in reversed(platform_store.list_audit(account.id)):
        deployment_id = (event.meta or {}).get("deployment_id", "")
        if event.action == "customer.provisioned" and deployment_id in by_id:
            return by_id[deployment_id]

    name = account.name.strip().lower()
    matches = [deployment for deployment in deployments if deployment.customer_name.strip().lower() == name]
    return matches[0] if len(matches) == 1 else None


def _readiness(deployment, backup, health, latest_rollout) -> str:
    if not deployment:
        return "not_deployed"
    if latest_rollout and latest_rollout.status in {"pending", "running", "paused"}:
        return "updating"
    if latest_rollout and latest_rollout.status == "failed":
        return "rollout_failed"
    if health and health.status == "failed":
        return "health_failed"
    if backup and backup.status == "failed":
        return "backup_failed"
    if health and health.status == "success":
        return "healthy"
    return "unknown"


def _status_count(job_summary, *statuses: str) -> int:
    return sum(int(job_summary.by_status.get(status, 0)) for status in statuses)


def _worker_signal(settings, job_summary) -> OperatorWorkerOut:
    pending = _status_count(job_summary, "queued", "retrying")
    running = _status_count(job_summary, "running")
    failed = _status_count(job_summary, "failed")
    if not settings.use_async_ingestion:
        status = "not_required"
    elif failed:
        status = "attention"
    elif pending:
        status = "backlog"
    elif running:
        status = "running"
    else:
        status = "clear"
    return OperatorWorkerOut(
        expected=settings.use_async_ingestion,
        pending_jobs=pending,
        running_jobs=running,
        failed_jobs=failed,
        status=status,
    )


def _security_signal(settings) -> OperatorSecurityOut:
    return OperatorSecurityOut(
        environment=settings.environment,
        production_like=settings.is_production_like,
        pgvector_required=settings.is_production_like,
        database_url_configured=bool(settings.database_url.strip()),
        rls_enforced=settings.rls_enforced,
        cookie_secure=settings.cookie_secure,
        pii_phase=settings.pii_phase,
    )


def _auth_signal(metrics: MonitoringSummary) -> OperatorAuthOut:
    return OperatorAuthOut(
        total_failures=metrics.auth_total,
        login_failures=metrics.login_failures,
        service_key_failures=metrics.service_key_failures,
        lockouts=metrics.lockouts,
        last_failure_at=metrics.last_auth_failure_at,
    )


def _api_signal(metrics: MonitoringSummary) -> OperatorApiOut:
    return OperatorApiOut(
        errors_5xx=metrics.api_errors_5xx,
        last_error_at=metrics.last_api_error_at,
        last_error_route=metrics.last_api_error_route,
        last_error_status=metrics.last_api_error_status,
    )


def _alert(
    *,
    id: str,
    severity: str,
    title: str,
    detail: str,
    action: str,
    signal: str,
) -> OperatorAlertOut:
    return OperatorAlertOut(
        id=id,
        severity=severity,
        title=title,
        detail=detail,
        action=action,
        signal=signal,
    )


def _alerts(
    security: OperatorSecurityOut,
    worker: OperatorWorkerOut,
    auth: OperatorAuthOut,
    api: OperatorApiOut,
) -> list[OperatorAlertOut]:
    alerts: list[OperatorAlertOut] = []
    if security.production_like and not security.database_url_configured:
        alerts.append(_alert(
            id="database-url-missing",
            severity="critical",
            title="Database URL missing",
            detail="A production-like OneBrain process must run against Postgres.",
            action="Set ONEBRAIN_DATABASE_URL before accepting traffic.",
            signal="database",
        ))
    if security.production_like and not security.rls_enforced:
        alerts.append(_alert(
            id="rls-not-enforced",
            severity="critical",
            title="RLS is not enforced",
            detail="Production-like environments must keep tenant isolation enforced at database level.",
            action="Set ONEBRAIN_RLS_ENFORCED=true and rerun the RLS validation.",
            signal="rls",
        ))
    if security.production_like and security.pii_phase != "dpia_signed":
        alerts.append(_alert(
            id="real-data-disabled",
            severity="warning",
            title="Real-data ingest is blocked",
            detail="The PII phase is not set to dpia_signed, so real personal data will be refused.",
            action="Use dpia_signed only for approved controlled real-data environments.",
            signal="privacy",
        ))
    if security.production_like and not security.cookie_secure:
        alerts.append(_alert(
            id="cookie-secure-disabled",
            severity="warning",
            title="Secure cookies disabled",
            detail="Session cookies should be HTTPS-only outside local development.",
            action="Set ONEBRAIN_COOKIE_SECURE=true on staging and production.",
            signal="auth",
        ))
    if worker.failed_jobs:
        alerts.append(_alert(
            id="job-failures",
            severity="warning",
            title="Failed jobs need review",
            detail=f"{worker.failed_jobs} jobs are currently marked failed.",
            action="Open recent failures, fix the cause, then retry or archive them.",
            signal="jobs",
        ))
    if worker.expected and worker.pending_jobs:
        alerts.append(_alert(
            id="worker-backlog",
            severity="warning",
            title="Worker backlog present",
            detail=f"{worker.pending_jobs} jobs are queued or retrying.",
            action="Check the worker /health endpoint and worker logs.",
            signal="worker",
        ))
    if auth.total_failures:
        alerts.append(_alert(
            id="auth-failures",
            severity="warning",
            title="Authentication failures observed",
            detail=f"{auth.total_failures} failed login or service-key auth attempts were observed by this process.",
            action="Review service-key usage and failed-login patterns.",
            signal="auth",
        ))
    if api.errors_5xx:
        alerts.append(_alert(
            id="api-errors",
            severity="warning",
            title="API 5xx errors observed",
            detail=f"{api.errors_5xx} server errors were observed by this process.",
            action="Inspect application logs for the reported route template.",
            signal="api",
        ))
    return alerts


@router.get("/observability", response_model=OperatorObservabilityOut)
def operator_observability(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    settings = get_settings()
    service_key_summary = get_service_key_store().summary()
    job_summary = get_job_store().summary(recent_failures_limit=10)
    metrics = monitoring_snapshot()
    security = _security_signal(settings)
    worker = _worker_signal(settings, job_summary)
    auth = _auth_signal(metrics)
    api = _api_signal(metrics)
    return OperatorObservabilityOut(
        generated_at=datetime.now(timezone.utc).isoformat(),
        runtime=OperatorRuntimeOut(
            vector_store=settings.vector_store,
            llm_provider=settings.llm_provider,
            embeddings_provider=settings.embeddings_provider,
            async_ingestion=settings.use_async_ingestion,
        ),
        retrieval=OperatorRetrievalOut(
            top_k=settings.top_k,
            min_score=settings.retrieval_min_score,
        ),
        storage=OperatorStorageOut(
            chunks=get_store().count(),
            intake_records=get_intake_store().count(),
        ),
        service_keys=OperatorServiceKeysOut(
            total=service_key_summary.total,
            active=service_key_summary.active,
            revoked=service_key_summary.revoked,
        ),
        jobs=OperatorJobsOut(
            total=job_summary.total,
            by_status=job_summary.by_status,
            by_type=job_summary.by_type,
            recent_failures=[
                OperatorJobFailureOut(
                    id=job.id,
                    type=job.type,
                    tenant_id=job.tenant_id,
                    account_id=job.account_id,
                    space_id=job.space_id,
                    attempts=job.attempts,
                    max_attempts=job.max_attempts,
                    error=job.error,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    completed_at=job.completed_at,
                )
                for job in job_summary.recent_failures
            ],
        ),
        security=security,
        worker=worker,
        auth=auth,
        api=api,
        alerts=_alerts(security, worker, auth, api),
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


@router.get("/accounts/{account_id}/service-keys", response_model=list[ServiceKeyInfo])
def list_account_service_keys(account_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _require_account(account_id)
    return [_service_key_out(k) for k in get_service_key_store().list_by_tenant(account_id)]


@router.delete("/accounts/{account_id}/service-keys/{key_id}")
def revoke_account_service_key(account_id: str, key_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _require_account(account_id)
    key = get_service_key_store().get(key_id)
    if not key or key.tenant_id != account_id:
        raise HTTPException(status_code=404, detail="Service key not found.")
    get_service_key_store().revoke(key_id)
    _record_operator_key_audit("service_key.revoked", principal, key)
    return {"revoked": key_id}


@router.get("/customers", response_model=list[CustomerOverviewOut])
def list_customers(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    platform_store = get_platform_store()
    control_store = get_control_plane_store()
    key_store = get_service_key_store()
    deployments = control_store.list_deployments()
    rows: list[CustomerOverviewOut] = []

    for account in platform_store.list_accounts():
        deployment = _deployment_for_account(account, deployments, platform_store)
        modules = control_store.list_modules(deployment.id) if deployment else []
        backup = control_store.latest_backup(deployment.id) if deployment else None
        health = control_store.latest_health(deployment.id) if deployment else None
        rollouts = control_store.list_rollouts(deployment.id) if deployment else []
        latest_rollout = sorted(rollouts, key=lambda r: r.created_at or r.id)[-1] if rollouts else None
        rows.append(CustomerOverviewOut(
            account=_account_out(account),
            spaces=[_space_out(space) for space in platform_store.list_spaces(account.id)],
            apps=[_app_out(app) for app in platform_store.list_app_installations(account.id)],
            brand_theme=_brand_theme_out(platform_store.resolve_brand_theme(account.id)),
            brand_themes=[_brand_theme_out(theme) for theme in platform_store.list_brand_themes(account.id)],
            service_keys=[_service_key_out(key) for key in key_store.list_by_tenant(account.id)],
            deployment=_deployment_out(deployment) if deployment else None,
            modules=[_module_out(module) for module in modules],
            backup=_backup_out(backup) if backup else None,
            health=_health_out(health) if health else None,
            latest_rollout=_rollout_out(latest_rollout) if latest_rollout else None,
            readiness=_readiness(deployment, backup, health, latest_rollout),
        ))

    return rows


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


@router.get("/deployments/{deployment_id}/backups/latest", response_model=BackupOut | None)
def latest_backup(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    backup = get_control_plane_store().latest_backup(deployment_id)
    return _backup_out(backup) if backup else None


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


@router.get("/deployments/{deployment_id}/health/latest", response_model=HealthOut | None)
def latest_health(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    health = get_control_plane_store().latest_health(deployment_id)
    return _health_out(health) if health else None


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


@router.patch("/rollouts/{rollout_id}", response_model=RolloutOut)
def update_rollout(rollout_id: str, body: RolloutStatusUpdate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    try:
        rollout = get_control_plane_store().update_rollout_status(
            rollout_id,
            body.status.strip(),
            notes=body.notes.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _rollout_out(rollout)

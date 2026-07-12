"""Operator control-plane endpoints.

These endpoints track deployment metadata and release state only. They do not
expose customer content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.account_access import authorize_account_admin, authorized_account_ids, is_account_admin
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
    get_fleet_store,
    get_intake_store,
    get_job_store,
    get_platform_store,
    get_provisioning_run_store,
    get_service_key_store,
    get_store,
)
from app.controlplane.rollout_exec import (
    build_rollout_dispatch_inputs,
    mark_rollout_dispatch_failed,
    resolve_railway_target,
    target_provider,
)
from app.controlplane.fleet_runner import plan_and_start_fleet_rollout, reconcile_fleet_rollout
from app.controlplane.pull_reconcile import reconcile_pull_targets
from app.controlplane.migration_lint import classify_release
from app.provisioning.runs import RolloutWorkflowDispatcher
from app.trust.release import (
    parse_registry_allowlist,
    release_signature_fields_from_body,
    verify_images,
    verify_release_signature,
)
from app.monitoring import MonitoringSummary, monitoring_snapshot
from app.platform.base import AuditEvent
from app.schemas import BrandThemeOut, ServiceKeyInfo

router = APIRouter(prefix="/api/operator", tags=["operator"])


class DeploymentCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    account_id: str = Field(default="", max_length=120)
    environment: str = Field(default="production", max_length=80)
    deployment_type: str = Field(default="dedicated_railway", max_length=80)
    region: str = Field(default="", max_length=80)
    release_ring: str = Field(default="manual", max_length=80)
    status: str = Field(default="active", max_length=80)
    current_version: str = Field(default="", max_length=80)
    current_migration: str = Field(default="", max_length=80)
    update_policy: str = Field(default="", max_length=20)
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
    update_policy: str = ""


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
    images: dict[str, str] = Field(default_factory=dict)
    rollback_kind: str = ""
    signature: str = ""
    signing_key_id: str = ""
    # P4-09: OPTIONAL NEW-migration delta shaped {"alembic": [[file, source], ...],
    # "sql": [[file, sql], ...]} classified at creation (grandfathering — pass only
    # the files new in THIS release). Inert when empty (today's behavior).
    migration_delta: dict = Field(default_factory=dict)
    # Reserved override knob. In P4 the linter classification is an ABSOLUTE floor:
    # a declared rollback_kind looser than the classification is refused whether or
    # not this is set (the binding test plan pins override -> still 400). A stricter
    # operator value is always allowed. Kept as declared API surface for Phase 5.
    rollback_kind_override: bool = False


class ReleaseOut(BaseModel):
    version: str
    git_sha: str
    modules: dict[str, str]
    migration_from: str = ""
    migration_to: str = ""
    security_notes: str = ""
    rollback_plan: str = ""
    status: str = "draft"
    images: dict[str, str] = Field(default_factory=dict)
    rollback_kind: str = ""
    signature: str = ""
    signing_key_id: str = ""


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
    rollback_kind: str = ""


class UpdatePolicyUpdate(BaseModel):
    update_policy: str = Field(min_length=1, max_length=20)


class RolloutCreate(BaseModel):
    target_version: str
    status: str = "pending"
    notes: str = ""
    ack_restore_required: bool = False
    id: str | None = None


class RolloutDispatch(BaseModel):
    callback_url: str = Field(min_length=1, max_length=500)
    dry_run: bool = True


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
    ack_restore_required: bool = False


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
    # Defense in depth: the operator surface is assembly-gated on is_operator_surface
    # (app/main.py), but refuse at request time too so a mis-wired customer stack can
    # never serve cross-account operator state even if the router is mounted.
    if not get_settings().is_operator_surface:
        raise HTTPException(status_code=404, detail="Not found.")
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can manage operator deployments.")


def _account_id_for_deployment(deployment_id: str) -> str | None:
    """Authoritatively map a deployment to its owning account for AUTHORIZATION.

    Prefers the deployment's authoritative account_id (set at provisioning /
    create time), else non-collidable signals. Returns None (caller fails closed)
    when unmapped."""
    deployment = get_control_plane_store().get_deployment(deployment_id)
    if deployment and deployment.account_id:
        return deployment.account_id
    return _account_id_from_signals(deployment_id)


def _account_id_from_signals(deployment_id: str) -> str | None:
    """Legacy fallback for deployments whose account_id is still '' — non-collidable
    signals only: the server-written `customer.provisioned` audit, then the
    `dep_{account_id}` convention. Deliberately NOT the customer_name display
    heuristic in _deployment_for_account, which an attacker could collide by naming
    an account after a victim deployment."""
    platform = get_platform_store()
    accounts = platform.list_accounts()
    for account in accounts:
        for event in platform.list_audit(account.id):
            if event.action == "customer.provisioned" and (event.meta or {}).get("deployment_id") == deployment_id:
                return account.id
    for account in accounts:
        if deployment_id == f"dep_{account.id}":
            return account.id
    return None


def _authorize_deployment(principal: Principal, deployment_id: str) -> None:
    """Unless on Mission Control (operator_mode), require the caller administer the
    account that owns this deployment. Same-404 as account access so a deployment
    id belonging to another account cannot be probed or acted on."""
    if get_settings().operator_mode:
        return
    authorize_account_admin(principal, _account_id_for_deployment(deployment_id) or "", get_platform_store())


def _authorize_rollout(principal: Principal, rollout_id: str) -> None:
    if get_settings().operator_mode:
        return
    control = get_control_plane_store()
    for dep in control.list_deployments():
        if any(r.id == rollout_id for r in control.list_rollouts(dep.id)):
            _authorize_deployment(principal, dep.id)
            return
    raise HTTPException(status_code=404, detail="Rollout not found.")


def _deployment_out(d: CustomerDeployment) -> DeploymentOut:
    return DeploymentOut(**{k: getattr(d, k) for k in DeploymentOut.model_fields})


def _module_out(m: DeploymentModule) -> ModuleOut:
    return ModuleOut(deployment_id=m.deployment_id, module_id=m.module_id, version=m.version, status=m.status)


def _release_out(r: ReleaseManifest) -> ReleaseOut:
    return ReleaseOut(
        version=r.version, git_sha=r.git_sha, modules=r.modules, migration_from=r.migration_from,
        migration_to=r.migration_to, security_notes=r.security_notes, rollback_plan=r.rollback_plan,
        status=r.status, images=r.images, rollback_kind=r.rollback_kind, signature=r.signature,
        signing_key_id=r.signing_key_id,
    )


def _backup_out(b: BackupRun) -> BackupOut:
    return BackupOut(id=b.id, deployment_id=b.deployment_id, status=b.status, detail=b.detail)


def _health_out(h: HealthCheckRun) -> HealthOut:
    return HealthOut(id=h.id, deployment_id=h.deployment_id, status=h.status, detail=h.detail)


def _plan_out(p: UpdatePlan) -> UpdatePlanOut:
    return UpdatePlanOut(
        deployment_id=p.deployment_id, target_version=p.target_version, allowed=p.allowed,
        reason=p.reason, current_modules=p.current_modules, target_modules=p.target_modules,
        modules_to_update=p.modules_to_update, rollback_kind=p.rollback_kind,
    )


def _rollout_out(r: RolloutRun) -> RolloutOut:
    return RolloutOut(
        id=r.id, deployment_id=r.deployment_id, target_version=r.target_version,
        status=r.status, started_by=r.started_by, notes=r.notes,
        ack_restore_required=r.ack_restore_required,
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
    # Recent job failures carry account_id / space_id / error text. On a customer
    # stack, scope them to accounts the caller administers so one account's admin
    # can't read another's failing-job details; Mission Control sees the fleet.
    recent_failures = job_summary.recent_failures
    if not settings.operator_mode:
        allowed = authorized_account_ids(principal, get_platform_store())
        recent_failures = [j for j in recent_failures if j.account_id in allowed]
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
                for job in recent_failures
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
    deployments = get_control_plane_store().list_deployments()
    if not get_settings().operator_mode:
        allowed = authorized_account_ids(principal, get_platform_store())
        # Use each row's account_id in hand; only legacy '' rows need the fallback.
        deployments = [d for d in deployments if (d.account_id or _account_id_from_signals(d.id)) in allowed]
    return [_deployment_out(d) for d in deployments]


@router.post("/deployments", response_model=DeploymentOut)
def create_deployment(body: DeploymentCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    account_id = body.account_id.strip()
    # Off Mission Control, a deployment must be tied to an account the caller
    # administers — otherwise create_deployment would be a way to register an
    # unscoped, fleet-visible record. The operator (operator_mode) creates for any
    # customer account.
    if not get_settings().operator_mode:
        authorize_account_admin(principal, account_id, get_platform_store())
    try:
        deployment = get_control_plane_store().create_deployment(CustomerDeployment(
            id=body.id or f"dep_{uuid4().hex[:12]}",
            customer_name=body.customer_name.strip(),
            account_id=account_id,
            environment=body.environment.strip(),
            deployment_type=body.deployment_type.strip(),
            region=body.region.strip(),
            release_ring=body.release_ring.strip(),
            status=body.status.strip(),
            current_version=body.current_version.strip(),
            current_migration=body.current_migration.strip(),
            update_policy=body.update_policy.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _deployment_out(deployment)


@router.get("/accounts/{account_id}/service-keys", response_model=list[ServiceKeyInfo])
def list_account_service_keys(account_id: str, principal: Principal = Depends(resolve_principal)):
    # Account-scoped: the caller must own or admin THIS account, not merely hold the
    # admin role — otherwise one account's admin could enumerate another's keys.
    authorize_account_admin(principal, account_id, get_platform_store())
    return [_service_key_out(k) for k in get_service_key_store().list_by_tenant(account_id)]


@router.delete("/accounts/{account_id}/service-keys/{key_id}")
def revoke_account_service_key(account_id: str, key_id: str, principal: Principal = Depends(resolve_principal)):
    # Account-scoped: revoking another account's keys is a cross-account availability
    # attack, so require ownership/admin membership in this specific account.
    authorize_account_admin(principal, account_id, get_platform_store())
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

    # Scope to accounts this admin actually owns/administers, EXCEPT on Mission
    # Control (operator_mode), where the operator legitimately oversees the whole
    # fleet. Without this, any account admin on a shared stack could enumerate
    # every other account's spaces, apps, and service-key metadata.
    operator_mode = get_settings().operator_mode
    for account in platform_store.list_accounts():
        if not operator_mode and not is_account_admin(principal, account, platform_store):
            continue
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
    _authorize_deployment(principal, deployment_id)
    return [_module_out(m) for m in get_control_plane_store().list_modules(deployment_id)]


@router.post("/deployments/{deployment_id}/modules", response_model=ModuleOut)
def upsert_module(deployment_id: str, body: ModuleUpsert, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
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


@router.post("/deployments/{deployment_id}/policy", response_model=DeploymentOut)
def set_update_policy(deployment_id: str, body: UpdatePolicyUpdate,
                      principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
    try:
        deployment = get_control_plane_store().set_update_policy(deployment_id, body.update_policy.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _deployment_out(deployment)


@router.get("/releases", response_model=list[ReleaseOut])
def list_releases(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    return [_release_out(r) for r in get_control_plane_store().list_releases()]


_ROLLBACK_RANK = {"code_only": 0, "restore_required": 1}
_MAX_DELTA_FILES = 100
_MAX_DELTA_CHARS = 200_000


def _rollback_rank(kind: str) -> int:
    # Unknown -> strictest, fail-closed (a garbled kind is later rejected by
    # validate_release; here it can never rank BELOW a real classification).
    return _ROLLBACK_RANK.get(kind, max(_ROLLBACK_RANK.values()))


def _delta_pairs(entries) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in list(entries or [])[:_MAX_DELTA_FILES]:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise HTTPException(status_code=400, detail="migration_delta entries must be [filename, source] pairs")
        out.append((str(entry[0])[:400], str(entry[1])[:_MAX_DELTA_CHARS]))
    return out


def _classify_migration_delta(delta: dict):
    if not isinstance(delta, dict):
        raise HTTPException(status_code=400, detail="migration_delta must be an object with 'alembic'/'sql' lists")
    return classify_release(
        alembic_sources=_delta_pairs(delta.get("alembic")),
        sql_files=_delta_pairs(delta.get("sql")),
    )


def _format_findings(findings) -> str:
    # Rule ids + SQL-only excerpts (the classifier already guarantees no data).
    return "; ".join(f"{f.rule}[{f.source}]: {f.excerpt}" for f in findings) or "no findings"


@router.post("/releases", response_model=ReleaseOut)
def create_release(body: ReleaseCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    settings = get_settings()
    errors: list[str] = []
    images = {k.strip(): v.strip() for k, v in body.images.items()}

    # P4-09: classify the NEW migration delta (when supplied) and reconcile it with
    # the declared rollback_kind. The classification is an ABSOLUTE floor: a declared
    # kind LOOSER than the classification is refused; an equal/stricter one stands;
    # an empty one is STAMPED with the classification. rollback_kind is inside the
    # signature payload, so a signed release must declare it explicitly (stamping
    # would break A6 re-verification). Inert when migration_delta is empty.
    resolved_rollback_kind = body.rollback_kind.strip()
    if body.migration_delta:
        classification = _classify_migration_delta(body.migration_delta)
        declared = body.rollback_kind.strip()
        if not declared:
            if body.signature.strip():
                raise HTTPException(status_code=400, detail=(
                    "a signed release must declare rollback_kind explicitly (it is inside the "
                    "signature payload); migration_delta cannot stamp it after signing"))
            resolved_rollback_kind = classification.rollback_kind
        elif _rollback_rank(declared) < _rollback_rank(classification.rollback_kind):
            raise HTTPException(status_code=400, detail=(
                f"rollback_kind disagrees with migration classification (declared {declared!r} is "
                f"looser than the classified {classification.rollback_kind!r}): "
                f"{_format_findings(classification.findings)}"))

    if settings.release_require_signed_images and not images:
        errors.append("release requires a digest-pinned images map")
    if images:
        # C7 / ground rule 1: the registry allowlist deliberately has NO off
        # flag — supplying an images map is itself the opt-in.
        errors += verify_images(images, parse_registry_allowlist(settings.release_registry_allowlist))
    if settings.release_require_rollback_kind and resolved_rollback_kind not in ("code_only", "restore_required"):
        errors.append("release requires rollback_kind (code_only|restore_required)")
    if body.signature or settings.release_require_signature:
        # A present signature is ALWAYS verified even when not required — a bad
        # signature must never be stored as if good. Verification runs over the
        # SAME stripped values that are persisted (A6), so the stored row
        # re-verifies later (P2 envelope computation, audits).
        if settings.release_require_signature and not body.signature:
            errors.append("release signature is required")
        elif not settings.release_verify_public_key:
            errors.append("release signature verification key is not configured")
        elif body.signature and not verify_release_signature(
                release_signature_fields_from_body(body), body.signature, settings.release_verify_public_key):
            errors.append("release signature verification failed")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
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
            images=images,
            rollback_kind=resolved_rollback_kind,
            signature=body.signature.strip(),
            signing_key_id=body.signing_key_id.strip(),
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _release_out(release)


@router.post("/deployments/{deployment_id}/backups", response_model=BackupOut)
def record_backup(deployment_id: str, body: BackupCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
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
    _authorize_deployment(principal, deployment_id)
    backup = get_control_plane_store().latest_backup(deployment_id)
    return _backup_out(backup) if backup else None


@router.post("/deployments/{deployment_id}/health", response_model=HealthOut)
def record_health(deployment_id: str, body: HealthCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
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
    _authorize_deployment(principal, deployment_id)
    health = get_control_plane_store().latest_health(deployment_id)
    return _health_out(health) if health else None


@router.get("/deployments/{deployment_id}/update-plan/{target_version}", response_model=UpdatePlanOut)
def update_plan(deployment_id: str, target_version: str, ack_restore_required: bool = False,
                principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
    return _plan_out(get_control_plane_store().plan_update(
        deployment_id, target_version, ack_restore_required=ack_restore_required))


@router.get("/deployments/{deployment_id}/rollouts", response_model=list[RolloutOut])
def list_rollouts(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
    return [_rollout_out(r) for r in get_control_plane_store().list_rollouts(deployment_id)]


@router.post("/deployments/{deployment_id}/rollouts", response_model=RolloutOut)
def start_rollout(deployment_id: str, body: RolloutCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
    try:
        rollout = get_control_plane_store().start_rollout(RolloutRun(
            id=body.id or f"roll_{uuid4().hex[:12]}",
            deployment_id=deployment_id,
            target_version=body.target_version.strip(),
            status=body.status.strip(),
            started_by=principal.user_id,
            notes=body.notes.strip(),
            ack_restore_required=body.ack_restore_required,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _rollout_out(rollout)


def offer_pull_target(control, rollout) -> None:
    """Hetzner/pull provider (H-9): the box converges on its OWN signed desired-state
    (P2/P3), so claim the rollout and mark it OFFERED — exec_status 'dispatched',
    dispatched_at now, request_payload flagged pull — WITHOUT calling any workflow
    dispatcher. The box pulls the desired-state and reports the outcome via its
    UpdateReport; reconcile_pull_targets synthesizes the terminal status. dispatched_at
    anchors the convergence deadline (H-8)."""
    if not control.claim_rollout_dispatch(rollout.id):
        return
    control.update_rollout_exec(rollout.id, dispatched_at=datetime.now(timezone.utc).isoformat(),
                                request_payload={"provider": "hetzner", "pull": True})


@router.post("/deployments/{deployment_id}/rollouts/{rollout_id}/dispatch", response_model=RolloutOut)
def dispatch_rollout(
    deployment_id: str,
    rollout_id: str,
    body: RolloutDispatch,
    principal: Principal = Depends(resolve_principal),
):
    """Dispatch a real update-customer workflow run for an existing (pending)
    rollout. Re-checks the plan_update safety gate, guards single-in-flight per
    deployment, and requires known Railway coordinates (fail-closed)."""
    _require_admin(principal)
    _authorize_deployment(principal, deployment_id)
    from app.routers.provisioning import _validate_callback_url

    control = get_control_plane_store()
    rollout = control.get_rollout(rollout_id)
    if not rollout or rollout.deployment_id != deployment_id:
        raise HTTPException(status_code=404, detail="Rollout not found.")
    if rollout.exec_status != "pending":
        raise HTTPException(status_code=409, detail="Rollout has already been dispatched.")
    active = control.list_active_rollout(deployment_id)
    if active and active.id != rollout_id:
        raise HTTPException(status_code=409, detail="Another rollout is already in progress for this deployment.")

    plan = control.plan_update(deployment_id, rollout.target_version,
                               ack_restore_required=rollout.ack_restore_required)
    if not plan.allowed:
        raise HTTPException(status_code=409, detail=f"Update blocked: {plan.reason}")
    release = control.get_release(rollout.target_version)
    deployment = control.get_deployment(deployment_id)
    if not release or not deployment:
        raise HTTPException(status_code=409, detail="Rollout target is no longer available.")

    _validate_callback_url(body.callback_url)
    try:
        railway = resolve_railway_target(get_provisioning_run_store(), deployment_id)
    except ValueError as exc:
        mark_rollout_dispatch_failed(control, rollout, str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if target_provider(railway) != "railway":
        # H-9 (was WP5 fail-closed): the GitHub/Railway workflow cannot act on a
        # Hetzner box, so OFFER the pull target instead of failing — the box
        # converges on its own signed desired-state and reports via its
        # UpdateReport; the reconcile tick resolves it. 200 (offered), no dispatch.
        offer_pull_target(control, rollout)
        return _rollout_out(control.get_rollout(rollout_id))

    # Atomically claim the pending rollout (compare-and-set exec_status
    # pending->dispatched) BEFORE the network dispatch, so two concurrent requests
    # can never both fire a real update job for the same rollout.
    if not control.claim_rollout_dispatch(rollout_id):
        raise HTTPException(status_code=409, detail="Rollout has already been dispatched.")

    settings = get_settings()
    inputs = build_rollout_dispatch_inputs(
        rollout=rollout, plan=plan, release=release, deployment=deployment, railway=railway,
        callback_url=body.callback_url, callback_key_id=settings.provisioning_callback_key_id,
        dry_run=body.dry_run,
    )
    try:
        workflow_url = RolloutWorkflowDispatcher(settings).dispatch(inputs)
    except RuntimeError as exc:
        mark_rollout_dispatch_failed(control, rollout, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    updated = control.update_rollout_exec(
        rollout_id, external_run_url=workflow_url,
        dispatched_at=datetime.now(timezone.utc).isoformat(),
        request_payload={"dry_run": body.dry_run},
    )
    return _rollout_out(updated)


# --- fleet rollouts (Phase 2: ring-by-ring fleet-wide update) ----------------

class FleetRolloutCreate(BaseModel):
    target_version: str = Field(min_length=1, max_length=120)
    callback_url: str = Field(min_length=1, max_length=500)
    failure_tolerance: int = Field(default=0, ge=0, le=10000)
    dry_run: bool = True


class FleetRolloutOut(BaseModel):
    id: str
    target_version: str
    status: str
    ring_order: list[str] = Field(default_factory=list)
    current_ring: str = ""
    failure_tolerance: int = 0
    started_by: str = ""
    notes: str = ""
    created_at: str = ""


class FleetRolloutPlanOut(BaseModel):
    waves: dict[str, list[str]] = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)
    blocked: dict[str, str] = Field(default_factory=dict)


class FleetRolloutCreateOut(BaseModel):
    fleet_rollout: FleetRolloutOut | None = None
    plan: FleetRolloutPlanOut


def _fleet_out(fr) -> FleetRolloutOut:
    return FleetRolloutOut(
        id=fr.id, target_version=fr.target_version, status=fr.status, ring_order=list(fr.ring_order),
        current_ring=fr.current_ring, failure_tolerance=fr.failure_tolerance,
        started_by=fr.started_by, notes=fr.notes, created_at=fr.created_at,
    )


def _fleet_plan_out(plan) -> FleetRolloutPlanOut:
    return FleetRolloutPlanOut(
        waves={w.ring: list(w.deployment_ids) for w in plan.waves},
        skipped=list(plan.skipped), blocked=dict(plan.blocked),
    )


def _require_operator_mode() -> None:
    # A fleet-wide sweep is a Mission Control capability over the whole fleet.
    if not get_settings().operator_mode:
        raise HTTPException(status_code=403, detail="Fleet rollouts require operator mode (Mission Control).")


def _dispatch_child_rollout(fleet_id: str, deployment_id: str, *, target_version: str,
                            callback_url: str, dry_run: bool) -> None:
    """Create and dispatch ONE child rollout for a fleet ring. A dispatch failure is
    recorded on the child (dispatch_failed, i.e. bookkeeping 'failed') so the fleet
    reducer counts it toward failure_tolerance; this never raises."""
    control = get_control_plane_store()
    settings = get_settings()
    child_id = f"roll_{uuid4().hex[:12]}"
    try:
        control.start_rollout(RolloutRun(
            id=child_id, deployment_id=deployment_id, target_version=target_version,
            status="pending", started_by=f"fleet:{fleet_id}", fleet_rollout_id=fleet_id))
    except ValueError:
        return  # plan blocked at start — the ring proceeds without this deployment
    rollout = control.get_rollout(child_id)
    if rollout is None:
        # The row vanished between start_rollout and the read-back — nothing
        # to mark failed, and dereferencing below would raise ("never raises").
        return
    release = control.get_release(target_version)
    deployment = control.get_deployment(deployment_id)
    # Children are created with the default ack_restore_required=False; fleet
    # sweeps can never auto-ack a restore_required release (the R3 guarantee).
    plan = control.plan_update(deployment_id, target_version,
                               ack_restore_required=rollout.ack_restore_required)
    if not (release and deployment and plan.allowed):
        mark_rollout_dispatch_failed(control, rollout, "update no longer available")
        return
    try:
        railway = resolve_railway_target(get_provisioning_run_store(), deployment_id)
    except ValueError as exc:
        mark_rollout_dispatch_failed(control, rollout, str(exc))
        return
    if target_provider(railway) != "railway":
        # H-9 (was WP5 fail-closed): OFFER the pull child instead of failing it —
        # the box converges on its own signed desired-state and the reconcile tick
        # resolves it from the box's UpdateReport. The child sits in-flight
        # (dispatched), not dispatch_failed.
        offer_pull_target(control, rollout)
        return
    if not control.claim_rollout_dispatch(child_id):
        return
    inputs = build_rollout_dispatch_inputs(
        rollout=rollout, plan=plan, release=release, deployment=deployment, railway=railway,
        callback_url=callback_url, callback_key_id=settings.provisioning_callback_key_id, dry_run=dry_run)
    try:
        workflow_url = RolloutWorkflowDispatcher(settings).dispatch(inputs)
    except RuntimeError as exc:
        mark_rollout_dispatch_failed(control, rollout, str(exc))
        return
    control.update_rollout_exec(
        child_id, external_run_url=workflow_url,
        dispatched_at=datetime.now(timezone.utc).isoformat(), request_payload={"dry_run": dry_run})


def fleet_dispatch_child(fleet_run, deployment_id) -> None:
    """dispatch_child bound to a persisted fleet rollout — reads callback_url/dry_run
    from it so create, callback-advance, and resume all dispatch the same way."""
    _dispatch_child_rollout(fleet_run.id, deployment_id, target_version=fleet_run.target_version,
                            callback_url=fleet_run.callback_url, dry_run=fleet_run.dry_run)


@router.post("/fleet-rollouts", response_model=FleetRolloutCreateOut)
def create_fleet_rollout(body: FleetRolloutCreate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _require_operator_mode()
    from app.routers.provisioning import _validate_callback_url
    _validate_callback_url(body.callback_url)
    control = get_control_plane_store()
    release = control.get_release(body.target_version)
    if not release:
        raise HTTPException(status_code=404, detail="No such release.")

    fleet_run, plan = plan_and_start_fleet_rollout(
        control, control, fleet_id=f"fleet_{uuid4().hex[:12]}", target_version=body.target_version,
        git_sha=release.git_sha, failure_tolerance=body.failure_tolerance,
        started_by=principal.user_id, created_at=datetime.now(timezone.utc).isoformat(),
        callback_url=body.callback_url, dry_run=body.dry_run, dispatch_child=fleet_dispatch_child)
    return FleetRolloutCreateOut(
        fleet_rollout=_fleet_out(fleet_run) if fleet_run else None, plan=_fleet_plan_out(plan))


@router.get("/fleet-rollouts", response_model=list[FleetRolloutOut])
def list_fleet_rollouts(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _require_operator_mode()
    return [_fleet_out(fr) for fr in get_control_plane_store().list_fleet_rollouts()]


@router.post("/fleet-rollouts/reconcile", response_model=list[FleetRolloutOut])
def reconcile_pull(principal: Principal = Depends(resolve_principal)):
    """The P4 driver for the pull-path reconcile tick (no scheduler in P4 — this
    operator-run endpoint or an external cron drives it, exactly as run_watchdog stayed
    test-only). Synthesizes each offered pull child's terminal status from its box's
    latest UpdateReport and feeds the UNCHANGED fleet reducer. At rest (no running fleet
    rollouts) it is a no-op. The reconcile scheduler/daemon is the Phase-5 infra tail."""
    _require_admin(principal)
    _require_operator_mode()
    control = get_control_plane_store()
    runs = reconcile_pull_targets(
        control, control, get_fleet_store().latest_heartbeats(),
        now=datetime.now(timezone.utc),
        deadline_seconds=get_settings().fleet_pull_convergence_deadline_seconds,
        dispatch_child=fleet_dispatch_child)
    return [_fleet_out(r) for r in runs]


@router.get("/fleet-rollouts/{fleet_id}", response_model=FleetRolloutOut)
def get_fleet_rollout(fleet_id: str, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _require_operator_mode()
    fr = get_control_plane_store().get_fleet_rollout(fleet_id)
    if not fr:
        raise HTTPException(status_code=404, detail="Fleet rollout not found.")
    return _fleet_out(fr)


def _fleet_transition(fleet_id: str, principal: Principal, *, to: str, allowed_from: set) -> FleetRolloutOut:
    _require_admin(principal)
    _require_operator_mode()
    control = get_control_plane_store()
    fr = control.get_fleet_rollout(fleet_id)
    if not fr:
        raise HTTPException(status_code=404, detail="Fleet rollout not found.")
    if fr.status not in allowed_from:
        raise HTTPException(status_code=409, detail=f"Cannot {to} a {fr.status} fleet rollout.")
    updated = control.update_fleet_rollout(fleet_id, status=to)
    # Resuming re-reconciles in case the current ring completed while paused.
    if to == "running":
        updated = reconcile_fleet_rollout(control, control, fleet_id, dispatch_child=fleet_dispatch_child) or updated
    return _fleet_out(updated)


@router.post("/fleet-rollouts/{fleet_id}/pause", response_model=FleetRolloutOut)
def pause_fleet_rollout(fleet_id: str, principal: Principal = Depends(resolve_principal)):
    return _fleet_transition(fleet_id, principal, to="paused", allowed_from={"running"})


@router.post("/fleet-rollouts/{fleet_id}/resume", response_model=FleetRolloutOut)
def resume_fleet_rollout(fleet_id: str, principal: Principal = Depends(resolve_principal)):
    return _fleet_transition(fleet_id, principal, to="running", allowed_from={"paused"})


@router.post("/fleet-rollouts/{fleet_id}/abort", response_model=FleetRolloutOut)
def abort_fleet_rollout(fleet_id: str, principal: Principal = Depends(resolve_principal)):
    return _fleet_transition(fleet_id, principal, to="aborted", allowed_from={"running", "paused"})


@router.patch("/rollouts/{rollout_id}", response_model=RolloutOut)
def update_rollout(rollout_id: str, body: RolloutStatusUpdate, principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _authorize_rollout(principal, rollout_id)
    try:
        rollout = get_control_plane_store().update_rollout_status(
            rollout_id,
            body.status.strip(),
            notes=body.notes.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _rollout_out(rollout)

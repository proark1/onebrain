"""Operator control-plane endpoints.

These endpoints track deployment metadata and release state only. They do not
expose customer content.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth.account_access import authorize_account_admin, authorized_account_ids, is_account_admin
from app.auth.principal import Principal, resolve_principal
from app.controlplane.base import (
    BackupRun,
    CustomerTeardownRequest,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    ReleasePromotion,
    ReleasePromotionEvent,
    RolloutRun,
    TEARDOWN_REQUEST_APPROVED,
    TEARDOWN_REQUEST_EXPIRED,
    UpdatePlan,
    is_operator_self_deployment,
    validate_teardown_request,
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
    SECRETS_EPOCH_PENDING_REASON,
    mark_rollout_dispatch_failed,
    resolve_pull_target,
)
from app.controlplane.development_gate import (
    DEVELOPMENT_GATE_CORE_MODULE_IDS,
    DEVELOPMENT_GATE_MODULE_IDS,
    DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS,
    is_current_replacement_bootstrap_failure,
    validate_module_transition,
)
from app.controlplane.desired_state import active_signer_in_served_set
from app.controlplane.fleet_runner import plan_and_start_fleet_rollout, reconcile_fleet_rollout
from app.controlplane.promotion import (
    attach_production_signature,
    manifest_digest,
    prepare_candidate,
    register_candidate,
    transition as transition_promotion,
)
from app.controlplane.reconcile_scheduler import reconcile_once
from app.controlplane.migration_lint import classify_release
from app.trust.envelope import compare_versions
from app.trust.release import (
    parse_registry_allowlist,
    release_signature_fields,
    release_signature_fields_from_body,
    verify_images,
    verify_release_signature,
)
from app.fleet.keys import verify_secret
from app.monitoring import MonitoringSummary, monitoring_snapshot
from app.platform.base import AuditEvent, scope_is_held
from app.schemas import BrandThemeOut, ServiceKeyInfo

router = APIRouter(prefix="/api/operator", tags=["operator"])


class DeploymentCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    account_id: str = Field(default="", max_length=120)
    environment: str = Field(default="production", max_length=80)
    deployment_type: str = Field(
        default="dedicated_server",
        max_length=80,
        pattern="^(dedicated_server|customer_owned)$",
    )
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
    created_at: str = ""
    is_release_gate: bool = False
    current_version_deployed_at: str = ""
    last_heartbeat_at: str = ""
    last_heartbeat_healthy: bool | None = None
    last_reported_version: str = ""
    last_reported_migration: str = ""


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


class PromotionEventOut(BaseModel):
    id: str
    action: str
    from_state: str = ""
    to_state: str
    actor: str = ""
    note: str = ""
    created_at: str = ""


class ReleasePromotionOut(BaseModel):
    state: str
    gate_deployment_id: str = ""
    dev_rollout_id: str = ""
    dev_started_at: str = ""
    dev_completed_at: str = ""
    dev_verified_at: str = ""
    production_signature_attached: bool = False
    customer_approved_at: str = ""
    customer_approved_by: str = ""
    customer_paused_at: str = ""
    customer_paused_reason: str = ""
    failure_reason: str = ""
    events: list[PromotionEventOut] = Field(default_factory=list)


class ReleaseOut(BaseModel):
    version: str
    git_sha: str
    modules: dict[str, str]
    migration_from: str = ""
    migration_to: str = ""
    security_notes: str = ""
    rollback_plan: str = ""
    status: str = "draft"
    created_at: str = ""
    images: dict[str, str] = Field(default_factory=dict)
    rollback_kind: str = ""
    signature: str = ""
    signing_key_id: str = ""
    promotion: ReleasePromotionOut | None = None


class ReleaseCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern="^(prepare|register)$")
    version: str = Field(min_length=1, max_length=120)
    git_sha: str = Field(min_length=1, max_length=120)
    modules: dict[str, str]
    images: dict[str, str]
    migration_from: str = ""
    migration_to: str = ""
    rollback_kind: str = "code_only"
    security_notes: str = ""
    rollback_plan: str = ""
    dev_signature: str = ""
    dev_signing_key_id: str = ""


class ReleaseCandidateOut(BaseModel):
    release: ReleaseOut
    manifest_digest: str
    created: bool = False
    dispatch_state: str = ""


class PromotionNote(BaseModel):
    note: str = Field(default="", max_length=1000)


class DevelopmentRetryIn(PromotionNote):
    ack_restore_required: bool = False


class ProductionSignatureIn(BaseModel):
    signature: str = Field(min_length=1, max_length=1000)
    signing_key_id: str = Field(min_length=1, max_length=120)


class DevelopmentGateOut(BaseModel):
    deployment: DeploymentOut | None = None
    ready: bool = False
    blockers: list[str] = Field(default_factory=list)


class DevelopmentGateProvisionIn(BaseModel):
    owner_email: str = Field(min_length=3, max_length=320)
    region: str = Field(default="nbg1", max_length=80)
    dry_run: bool = True


class DevelopmentGatePreparationOut(BaseModel):
    deployment_id: str
    updated: bool
    secrets_epoch: int
    applied_secrets_epoch: int
    ready: bool
    blockers: list[str] = Field(default_factory=list)


DEVELOPMENT_GATE_DEPLOYMENT_ID = "onebrain_development_gate"
DEVELOPMENT_GATE_ACCOUNT_ID = "onebrain-development"


class BackupCreate(BaseModel):
    status: str
    detail: str = ""
    id: str | None = None


class BackupOut(BaseModel):
    id: str
    deployment_id: str
    status: str
    detail: str = ""
    created_at: str = ""


class HealthCreate(BaseModel):
    status: str
    detail: str = ""
    id: str | None = None


class HealthOut(BaseModel):
    id: str
    deployment_id: str
    status: str
    detail: str = ""
    created_at: str = ""


class UpdatePlanOut(BaseModel):
    deployment_id: str
    target_version: str
    allowed: bool
    reason: str
    current_modules: dict[str, str] = Field(default_factory=dict)
    target_modules: dict[str, str] = Field(default_factory=dict)
    modules_to_update: dict[str, str] = Field(default_factory=dict)
    rollback_kind: str = ""
    warnings: list[str] = Field(default_factory=list)


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
    created_at: str = ""
    exec_status: str = "pending"
    external_provider: str = ""
    external_run_id: str = ""
    external_run_url: str = ""
    failure_reason: str = ""
    dispatched_at: str = ""
    completed_at: str = ""
    fleet_rollout_id: str = ""
    ack_restore_required: bool = False
    target_source: str = ""


class CustomerTeardownRequestCreate(BaseModel):
    """Evidence required to open a record-only teardown approval request."""
    legal_hold_evidence_ref: str = Field(min_length=1, max_length=500)
    backup_retention_evidence_ref: str = Field(min_length=1, max_length=500)


class CustomerTeardownApproval(BaseModel):
    # Empty values are handled in the endpoint so the denied attempt is audited.
    nonce: str = Field(default="", max_length=500)


class CustomerTeardownRequestOut(BaseModel):
    id: str
    deployment_id: str
    account_id: str
    legal_hold_evidence_ref: str
    backup_retention_evidence_ref: str
    requested_by: str
    approver_ids: list[str] = Field(default_factory=list)
    nonce_expires_at: str
    status: str
    execution_result: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class CustomerTeardownRequestCreatedOut(BaseModel):
    request: CustomerTeardownRequestOut
    # Returned exactly once at creation; only its SHA-256 hash is persisted.
    approval_nonce: str


class CustomerTeardownExecute(BaseModel):
    # Typed copy-the-phrase confirmation, re-checked server-side (mirrors the
    # users-panel delete confirm). Must equal "decommission <deployment_id>".
    confirmation_phrase: str = Field(default="", max_length=200)


class CustomerTeardownExecutedOut(BaseModel):
    request: CustomerTeardownRequestOut
    record_only: bool = False        # True when no infrastructure was touched
    warning: str = ""
    servers_deleted: list[str] = Field(default_factory=list)
    volumes_deleted: list[str] = Field(default_factory=list)
    firewalls_deleted: list[str] = Field(default_factory=list)
    dns_deleted: list[str] = Field(default_factory=list)
    fleet_keys_revoked: int = 0


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


def _release_out(
    r: ReleaseManifest,
    promotion: ReleasePromotion | None = None,
    events: list[ReleasePromotionEvent] | None = None,
) -> ReleaseOut:
    promotion_out = None
    if promotion:
        promotion_out = ReleasePromotionOut(
            state=promotion.state,
            gate_deployment_id=promotion.gate_deployment_id,
            dev_rollout_id=promotion.dev_rollout_id,
            dev_started_at=promotion.dev_started_at,
            dev_completed_at=promotion.dev_completed_at,
            dev_verified_at=promotion.dev_verified_at,
            production_signature_attached=bool(r.signature),
            customer_approved_at=promotion.customer_approved_at,
            customer_approved_by=promotion.customer_approved_by,
            customer_paused_at=promotion.customer_paused_at,
            customer_paused_reason=promotion.customer_paused_reason,
            failure_reason=promotion.failure_reason,
            events=[PromotionEventOut(**{
                key: getattr(event, key)
                for key in PromotionEventOut.model_fields
            }) for event in (events or [])],
        )
    return ReleaseOut(
        version=r.version, git_sha=r.git_sha, modules=r.modules, migration_from=r.migration_from,
        migration_to=r.migration_to, security_notes=r.security_notes, rollback_plan=r.rollback_plan,
        status=r.status, created_at=r.created_at, images=r.images, rollback_kind=r.rollback_kind, signature=r.signature,
        signing_key_id=r.signing_key_id,
        promotion=promotion_out,
    )


def _backup_out(b: BackupRun) -> BackupOut:
    return BackupOut(
        id=b.id,
        deployment_id=b.deployment_id,
        status=b.status,
        detail=b.detail,
        created_at=b.created_at,
    )


def _health_out(h: HealthCheckRun) -> HealthOut:
    return HealthOut(
        id=h.id,
        deployment_id=h.deployment_id,
        status=h.status,
        detail=h.detail,
        created_at=h.created_at,
    )


def _plan_out(p: UpdatePlan) -> UpdatePlanOut:
    return UpdatePlanOut(
        deployment_id=p.deployment_id, target_version=p.target_version, allowed=p.allowed,
        reason=p.reason, current_modules=p.current_modules, target_modules=p.target_modules,
        modules_to_update=p.modules_to_update, rollback_kind=p.rollback_kind,
        warnings=p.warnings,
    )


def _rollout_out(r: RolloutRun) -> RolloutOut:
    return RolloutOut(
        id=r.id, deployment_id=r.deployment_id, target_version=r.target_version,
        status=r.status, started_by=r.started_by, notes=r.notes,
        created_at=r.created_at,
        exec_status=r.exec_status,
        external_provider=r.external_provider,
        external_run_id=r.external_run_id,
        external_run_url=r.external_run_url,
        failure_reason=r.failure_reason,
        dispatched_at=r.dispatched_at,
        completed_at=r.completed_at,
        fleet_rollout_id=r.fleet_rollout_id,
        ack_restore_required=r.ack_restore_required,
        target_source=str((r.request_payload or {}).get("target_source", "")),
    )


_TEARDOWN_APPROVAL_NONCE_TTL = timedelta(minutes=15)


def _teardown_request_out(request: CustomerTeardownRequest) -> CustomerTeardownRequestOut:
    """Return protocol state without the nonce hash or any raw approval secret."""
    return CustomerTeardownRequestOut(
        id=request.id,
        deployment_id=request.deployment_id,
        account_id=request.account_id,
        legal_hold_evidence_ref=request.legal_hold_evidence_ref,
        backup_retention_evidence_ref=request.backup_retention_evidence_ref,
        requested_by=request.requested_by,
        approver_ids=list(request.approver_ids),
        nonce_expires_at=request.nonce_expires_at,
        status=request.status,
        execution_result=request.execution_result,
        created_at=request.created_at,
        updated_at=request.updated_at,
        completed_at=request.completed_at,
    )


def _record_teardown_audit(
    principal: Principal,
    *,
    account_id: str,
    deployment_id: str,
    request_id: str,
    action: str,
    decision: str,
    meta: dict | None = None,
) -> None:
    """Record intent/review only; never put raw approval nonces in the audit log."""
    get_platform_store().record_audit(AuditEvent(
        id=f"aud_teardown_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type="customer_teardown_request",
        target_id=request_id or deployment_id,
        purpose="customer_teardown",
        decision=decision,
        meta={"deployment_id": deployment_id, **(meta or {})},
    ))


def _teardown_target(deployment_id: str, principal: Principal):
    """Resolve the authorized deployment/account binding for a teardown record."""
    control = get_control_plane_store()
    deployment = control.get_deployment(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found.")
    _authorize_deployment(principal, deployment_id)
    account_id = deployment.account_id or _account_id_from_signals(deployment_id)
    if not account_id:
        raise HTTPException(status_code=409, detail="Deployment account binding is required for teardown review.")
    platform = get_platform_store()
    if not platform.get_account(account_id):
        raise HTTPException(status_code=409, detail="Deployment account is not available for teardown review.")
    return control, deployment, account_id, platform


_TEARDOWN_MANIFEST_SCALAR_KEYS = ("server_id", "dns_record_id", "firewall_id")


def _resolve_erasure_manifest(deployment_id: str) -> dict:
    """Accumulate the COMPLETE Hetzner erasure manifest across ALL of a deployment's
    provisioning runs. An idempotent-reuse run carries the reused server_id but EMPTY
    volume/DNS/firewall ids; only the original creating run holds those, so the latest
    run alone under-reports. First-non-empty wins for the scalar ids (the broker
    guarantees one server per deployment); volume ids are unioned. This drives the
    real-teardown-vs-record-only decision and enriches the audit — the broker itself
    discovers what to delete by label, never from these ids."""
    runs = get_provisioning_run_store().list_runs(deployment_id=deployment_id)
    merged: dict = {"server_id": "", "volume_ids": [], "dns_record_id": "", "firewall_id": ""}
    seen_volumes: set[str] = set()
    for run in sorted(runs, key=lambda r: (r.created_at, r.id)):  # oldest first
        manifest = (run.result_payload or {}).get("erasure_manifest", {}) or {}
        for key in _TEARDOWN_MANIFEST_SCALAR_KEYS:
            if not merged[key] and manifest.get(key):
                merged[key] = manifest[key]
        for volume_id in (manifest.get("volume_ids") or []):
            if volume_id and volume_id not in seen_volumes:
                seen_volumes.add(volume_id)
                merged["volume_ids"].append(volume_id)
    return merged


def _manifest_has_resources(manifest: dict) -> bool:
    return bool(
        manifest.get("server_id") or manifest.get("volume_ids")
        or manifest.get("dns_record_id") or manifest.get("firewall_id")
    )


def _revoke_deployment_fleet_keys(deployment_id: str) -> int:
    """Revoke every active fleet key for a deployment so a resurrected box cannot
    heartbeat, pull desired state, or re-fetch its secret bundle. Mirrors the
    re-enrollment rotation loop in app/routers/fleet.py. Bootstrap tokens auto-expire
    and the sealed bundle is served only to a valid ACTIVE key, so scrubbing those is
    a documented Phase-B hygiene follow-up, not required for correctness."""
    fleet_store = get_fleet_store()
    revoked = 0
    for key in fleet_store.list_keys(deployment_id):
        if key.status == "active" and fleet_store.revoke_key(key.id):
            revoked += 1
    return revoked


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
        latest_rollout = max(rollouts, key=lambda rollout: (rollout.created_at, rollout.id)) if rollouts else None
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


@router.post(
    "/deployments/{deployment_id}/teardown-requests",
    response_model=CustomerTeardownRequestCreatedOut,
    status_code=201,
)
def create_customer_teardown_request(
    deployment_id: str,
    body: CustomerTeardownRequestCreate,
    principal: Principal = Depends(resolve_principal),
):
    """Open a review record only; this endpoint has no deletion capability."""
    _require_admin(principal)
    control, deployment, account_id, platform = _teardown_target(deployment_id, principal)
    if scope_is_held(platform.list_legal_holds(account_id)):
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id="",
            action="customer_teardown.request_denied",
            decision="denied_legal_hold",
            meta={"reason": "active_legal_hold"},
        )
        raise HTTPException(
            status_code=409,
            detail="This account is under an active legal hold and cannot enter teardown review.",
        )

    now = datetime.now(timezone.utc)
    approval_nonce = secrets.token_urlsafe(32)
    request = CustomerTeardownRequest(
        id=f"tear_{uuid4().hex[:12]}",
        deployment_id=deployment.id,
        account_id=account_id,
        nonce_hash=hashlib.sha256(approval_nonce.encode("utf-8")).hexdigest(),
        nonce_expires_at=(now + _TEARDOWN_APPROVAL_NONCE_TTL).isoformat(),
        legal_hold_evidence_ref=body.legal_hold_evidence_ref.strip(),
        backup_retention_evidence_ref=body.backup_retention_evidence_ref.strip(),
        requested_by=principal.user_id,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    try:
        stored = control.create_teardown_request(request)
    except ValueError as exc:
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id=request.id,
            action="customer_teardown.request_denied",
            decision="denied",
            meta={"reason": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _record_teardown_audit(
        principal,
        account_id=account_id,
        deployment_id=deployment.id,
        request_id=stored.id,
        action="customer_teardown.request_created",
        decision="recorded",
        meta={
            "legal_hold_evidence_ref": stored.legal_hold_evidence_ref,
            "backup_retention_evidence_ref": stored.backup_retention_evidence_ref,
            "nonce_expires_at": stored.nonce_expires_at,
            "execution": "disabled",
        },
    )
    return CustomerTeardownRequestCreatedOut(
        request=_teardown_request_out(stored),
        approval_nonce=approval_nonce,
    )


@router.post(
    "/deployments/{deployment_id}/teardown-requests/{request_id}/approvals",
    response_model=CustomerTeardownRequestOut,
)
def approve_customer_teardown_request(
    deployment_id: str,
    request_id: str,
    body: CustomerTeardownApproval,
    principal: Principal = Depends(resolve_principal),
):
    """Record an independent approval; terminal state explicitly disables execution."""
    _require_admin(principal)
    control, deployment, account_id, platform = _teardown_target(deployment_id, principal)
    request = control.get_teardown_request(request_id)
    if not request or request.deployment_id != deployment.id:
        raise HTTPException(status_code=404, detail="Teardown request not found.")
    if request.account_id != account_id:
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id=request.id,
            action="customer_teardown.approval_denied",
            decision="denied_binding_mismatch",
            meta={"reason": "deployment_account_binding_mismatch"},
        )
        raise HTTPException(status_code=409, detail="Teardown request account binding does not match the deployment.")
    if scope_is_held(platform.list_legal_holds(account_id)):
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id=request.id,
            action="customer_teardown.approval_denied",
            decision="denied_legal_hold",
            meta={"reason": "active_legal_hold"},
        )
        raise HTTPException(
            status_code=409,
            detail="This account is under an active legal hold and cannot approve teardown review.",
        )

    raw_nonce = body.nonce.strip()
    if not raw_nonce:
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id=request.id,
            action="customer_teardown.approval_denied",
            decision="denied",
            meta={"reason": "missing_nonce"},
        )
        raise HTTPException(status_code=400, detail="An approval nonce is required.")

    try:
        updated = control.approve_teardown_request(
            request.id,
            approver_id=principal.user_id,
            nonce_hash=hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest(),
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
    except ValueError as exc:
        reason = str(exc)
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id=request.id,
            action="customer_teardown.approval_denied",
            decision="denied",
            meta={"reason": reason},
        )
        status_code = 400 if reason == "teardown approval nonce is invalid." else 409
        raise HTTPException(status_code=status_code, detail=reason) from exc

    if updated.status == TEARDOWN_REQUEST_EXPIRED:
        _record_teardown_audit(
            principal,
            account_id=account_id,
            deployment_id=deployment.id,
            request_id=updated.id,
            action="customer_teardown.approval_denied",
            decision="denied_expired",
            meta={"reason": "nonce_expired", "execution_result": updated.execution_result},
        )
        raise HTTPException(status_code=409, detail="The teardown approval nonce has expired.")

    _record_teardown_audit(
        principal,
        account_id=account_id,
        deployment_id=deployment.id,
        request_id=updated.id,
        action=(
            "customer_teardown.approved"
            if updated.status == TEARDOWN_REQUEST_APPROVED
            else "customer_teardown.approval_recorded"
        ),
        decision=("approved" if updated.status == TEARDOWN_REQUEST_APPROVED else "recorded"),
        meta={
            "approver_count": len(updated.approver_ids),
            "status": updated.status,
            "execution_result": updated.execution_result,
        },
    )
    return _teardown_request_out(updated)


@router.post(
    "/deployments/{deployment_id}/teardown-requests/{request_id}/execute",
    response_model=CustomerTeardownExecutedOut,
)
def execute_customer_teardown_request(
    deployment_id: str,
    request_id: str,
    body: CustomerTeardownExecute,
    principal: Principal = Depends(resolve_principal),
):
    """Execute an APPROVED teardown: destroy the box's Hetzner infrastructure through
    the broker (or record-only tombstone when nothing remains), revoke the box's fleet
    keys, and tombstone the deployment. operator_mode-only — it reaches the broker, so
    it mirrors the fleet router's Mission-Control mount gate (a customer console gets
    a 404, never a hint that the endpoint exists)."""
    _require_admin(principal)
    settings = get_settings()
    if not settings.operator_mode:
        raise HTTPException(status_code=404, detail="Teardown execution is not available on this deployment.")
    control, deployment, account_id, platform = _teardown_target(deployment_id, principal)
    request = control.get_teardown_request(request_id)
    if not request or request.deployment_id != deployment.id:
        raise HTTPException(status_code=404, detail="Teardown request not found.")

    def _deny(decision: str, detail: str, status_code: int, meta: dict | None = None) -> HTTPException:
        _record_teardown_audit(
            principal, account_id=account_id, deployment_id=deployment.id,
            request_id=request.id, action="customer_teardown.execution_denied",
            decision=decision, meta=meta,
        )
        return HTTPException(status_code=status_code, detail=detail)

    if request.account_id != account_id:
        raise _deny("denied_binding_mismatch",
                    "Teardown request account binding does not match the deployment.", 409,
                    {"reason": "deployment_account_binding_mismatch"})
    if request.status != TEARDOWN_REQUEST_APPROVED:
        raise _deny("denied_not_approved",
                    "Teardown request is not approved for execution.", 409,
                    {"status": request.status})
    # Dual-control TOCTOU: an APPROVED row may have reached the threshold under a
    # temporarily relaxed policy (min_approvals=1 / self-approval). Re-validate the
    # approvals against the CURRENT settings, so reverting to the strict defaults still
    # blocks a single-approval (or self-approved) teardown from executing.
    try:
        validate_teardown_request(request)
    except ValueError as exc:
        raise _deny("denied_policy_regressed",
                    f"Teardown approvals no longer satisfy the dual-control policy: {exc}", 409,
                    {"reason": str(exc)})
    # The development release gate must not be decommissioned out from under release
    # promotion — get_release_gate would otherwise be left pointing at a destroyed box.
    if deployment.is_release_gate:
        raise _deny("denied_release_gate",
                    "Cannot decommission the active release gate; re-designate the gate first.", 409,
                    {"reason": "active_release_gate"})
    # TOCTOU: re-check the legal hold at execute time, not just at request/approve.
    if scope_is_held(platform.list_legal_holds(account_id)):
        raise _deny("denied_legal_hold",
                    "This account is under an active legal hold and cannot be decommissioned.", 409,
                    {"reason": "active_legal_hold"})
    # Typed copy-the-phrase confirmation, re-checked server-side.
    expected_phrase = f"decommission {deployment.id}"
    if body.confirmation_phrase.strip() != expected_phrase:
        raise _deny("denied_phrase_mismatch",
                    f"Type '{expected_phrase}' exactly to confirm decommission.", 400,
                    {"reason": "confirmation_phrase_mismatch"})

    # Broker-only token boundary (P4-01): teardown reaches Hetzner ONLY through the
    # out-of-process broker. Refuse to fall back to an in-process broker (which would
    # use a local Hetzner token inside the API process) unless the explicit dogfood
    # escape hatch is set — mirror build_hetzner_broker's production guard.
    if not (getattr(settings, "hetzner_broker_url", "")
            or getattr(settings, "hetzner_allow_inprocess_broker", False)):
        raise HTTPException(
            status_code=502,
            detail="Teardown requires the out-of-process Hetzner broker (set ONEBRAIN_HETZNER_BROKER_URL).")

    # Audit enrichment only — the broker discovers what to delete by label, never from
    # these ids, and the record-only decision comes from the broker's own response.
    manifest = _resolve_erasure_manifest(deployment.id)

    from app.provisioning.hetzner.broker import build_hetzner_broker

    destroy_result = None
    broker_error = ""
    try:
        destroy_result = build_hetzner_broker(settings).destroy_box(deployment.id, confirm=True)
    except (RuntimeError, OSError, ValueError) as exc:
        broker_error = str(exc)

    now_iso = datetime.now(timezone.utc).isoformat()

    if destroy_result is None:
        # FAIL CLOSED on ANY broker error, regardless of the manifest. A missing/empty
        # erasure manifest does NOT prove no infrastructure exists (a legacy/imported
        # box may hold unrecorded resources), and a transient broker outage must never
        # let us tombstone + revoke keys while a live server may remain billed and
        # reachable. Record-only is taken ONLY on a SUCCESSFUL broker response reporting
        # nothing_found. The request becomes terminal; retry via a fresh request (the
        # discovery-scoped destroy is idempotent, so a re-run finishes any partial).
        control.record_teardown_execution(
            request.id, succeeded=False,
            result=f"broker unavailable: {broker_error}", executed_at=now_iso)
        _record_teardown_audit(
            principal, account_id=account_id, deployment_id=deployment.id,
            request_id=request.id, action="customer_teardown.execution_failed",
            decision="broker_unavailable",
            meta={"error": broker_error, "has_recorded_infra": _manifest_has_resources(manifest)})
        raise HTTPException(
            status_code=502,
            detail=f"Teardown could not reach the infrastructure broker: {broker_error}")

    record_only = bool(destroy_result.nothing_found)
    warning = ("No infrastructure was touched — nothing remained for this deployment."
               if record_only else "")

    keys_revoked = _revoke_deployment_fleet_keys(deployment.id)
    control.remove_deployment(deployment.id, removed_at=now_iso)

    deleted = {
        "servers": list(destroy_result.servers_deleted) if destroy_result else [],
        "volumes": list(destroy_result.volumes_deleted) if destroy_result else [],
        "firewalls": list(destroy_result.firewalls_deleted) if destroy_result else [],
        "dns": list(destroy_result.dns_deleted) if destroy_result else [],
    }
    if record_only:
        summary = f"record-only: no infrastructure deleted; fleet keys revoked ({keys_revoked})"
    else:
        summary = (
            "infrastructure destroyed — "
            f"servers={len(deleted['servers'])} volumes={len(deleted['volumes'])} "
            f"firewalls={len(deleted['firewalls'])} dns={len(deleted['dns'])}; "
            f"fleet keys revoked ({keys_revoked})"
        )
    executed = control.record_teardown_execution(
        request.id, succeeded=True, result=summary, executed_at=now_iso)

    _record_teardown_audit(
        principal, account_id=account_id, deployment_id=deployment.id, request_id=executed.id,
        action="customer_teardown.executed",
        decision="record_only" if record_only else "infrastructure_destroyed",
        meta={
            "record_only": record_only,
            "deleted": deleted,
            "fleet_keys_revoked": keys_revoked,
            "expected_manifest": manifest,
            "warning": warning,
        },
    )
    return CustomerTeardownExecutedOut(
        request=_teardown_request_out(executed),
        record_only=record_only,
        warning=warning,
        servers_deleted=deleted["servers"],
        volumes_deleted=deleted["volumes"],
        firewalls_deleted=deleted["firewalls"],
        dns_deleted=deleted["dns"],
        fleet_keys_revoked=keys_revoked,
    )


@router.get("/releases", response_model=list[ReleaseOut])
def list_releases(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    store = get_control_plane_store()
    if not get_settings().operator_mode:
        return [_release_out(release) for release in store.list_releases()]
    promotions = {promotion.release_version: promotion for promotion in store.list_release_promotions()}
    return [
        _release_out(
            release,
            promotions.get(release.version),
            store.list_release_promotion_events(release.version) if release.version in promotions else [],
        )
        for release in store.list_releases()
    ]


def _require_candidate_auth(authorization: str, key_id: str) -> str:
    settings = get_settings()
    if not settings.operator_mode:
        raise HTTPException(status_code=404, detail="Release candidate endpoint is not available.")
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if (
        not token
        or not key_id
        or key_id != settings.release_candidate_key_id
        or not settings.release_candidate_key_hash
        or not verify_secret(token, settings.release_candidate_key_hash)
    ):
        raise HTTPException(status_code=401, detail="Invalid release candidate credential.")
    return f"candidate:{key_id}"


def _latest_approved_release(store) -> ReleaseManifest | None:
    approved = [
        promotion for promotion in store.list_release_promotions()
        if promotion.state in {"customer_approved", "customer_paused"}
        and promotion.customer_approved_at
    ]
    approved.sort(
        key=lambda promotion: (promotion.customer_approved_at, promotion.release_version),
        reverse=True,
    )
    if approved:
        return store.get_release(approved[0].release_version)
    # Bootstrap only: before the first promotion row exists, accept the newest
    # active legacy manifest only when its offline production signature verifies.
    production_key = getattr(get_settings(), "release_verify_public_key", "")
    trusted = [
        release for release in store.list_releases()
        if release.status == "active"
        and release.signature
        and production_key
        and verify_release_signature(release_signature_fields(release), release.signature, production_key)
    ]
    trusted.sort(key=lambda release: (release.created_at, release.version), reverse=True)
    return trusted[0] if trusted else None


def _release_covers_development_gate(
    release: ReleaseManifest | None,
    module_ids,
    *,
    registry_allowlist: str,
    exact: bool,
) -> bool:
    if not release:
        return False
    required = frozenset(module_ids)
    modules = frozenset((release.modules or {}).keys())
    images = frozenset((release.images or {}).keys())
    if exact:
        if modules != required or images != required:
            return False
    elif not required.issubset(modules) or not required.issubset(images):
        return False
    selected_images = {module_id: release.images[module_id] for module_id in required}
    return not verify_images(selected_images, parse_registry_allowlist(registry_allowlist))


def _replacement_development_gate_baseline(
    store,
    module_ids,
    *,
    settings,
) -> ReleaseManifest | None:
    """Select a failed dev candidate solely to seed a replacement gate."""
    required = frozenset(module_ids)
    gate = store.get_release_gate()
    if (
        required != DEVELOPMENT_GATE_MODULE_IDS
        or gate is None
        or gate.environment != "development"
        or gate.deployment_type != "dedicated_server"
        or gate.status != "active"
    ):
        return None
    active_modules = {
        module.module_id
        for module in store.list_modules(gate.id)
        if module.status == "active"
    }
    if active_modules != DEVELOPMENT_GATE_CORE_MODULE_IDS:
        return None
    development_key = getattr(settings, "dev_release_verify_public_key", "")
    if not development_key:
        return None

    candidates: list[tuple[str, str, ReleaseManifest]] = []
    for promotion in store.list_release_promotions():
        if (
            promotion.state != "dev_failed"
            or promotion.gate_deployment_id != gate.id
            or promotion.failure_reason != "dev_preflight_failed"
        ):
            continue
        if not is_current_replacement_bootstrap_failure(
            promotion,
            store.list_release_promotion_events(promotion.release_version),
            gate_deployment_id=gate.id,
        ):
            continue
        release = store.get_release(promotion.release_version)
        if (
            not release
            or release.status == "yanked"
            or not _release_covers_development_gate(
                release,
                required,
                registry_allowlist=settings.release_registry_allowlist,
                exact=True,
            )
            or not promotion.dev_signature
            or not verify_release_signature(
                release_signature_fields(release),
                promotion.dev_signature,
                development_key,
            )
        ):
            continue
        candidates.append((promotion.updated_at, promotion.release_version, release))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2] if candidates else None


def _development_gate_provisioning_baseline(
    store,
    module_ids,
    *,
    settings,
) -> tuple[ReleaseManifest | None, str]:
    approved = _latest_approved_release(store)
    if _release_covers_development_gate(
        approved,
        module_ids,
        registry_allowlist=settings.release_registry_allowlist,
        exact=False,
    ):
        return approved, "approved_release"
    replacement = _replacement_development_gate_baseline(
        store,
        module_ids,
        settings=settings,
    )
    if replacement:
        return replacement, "development_replacement_candidate"
    return approved, "approved_release" if approved else ""


def _replacement_development_gate_seed_is_trusted(
    store,
    gate: CustomerDeployment,
    release: ReleaseManifest | None,
    *,
    settings,
) -> bool:
    """Verify the durable evidence for a provisioned replacement gate.

    A replacement is intentionally seeded from the development-signed release
    that the legacy Core-only gate could not run.  Keep that exception confined
    to the generated replacement identity and require the successful provision
    record that installed the exact signed candidate.  This evidence remains
    valid after the release-gate marker moves to the replacement.
    """
    if (
        release is None
        or release.status == "yanked"
        or not gate.id.startswith(DEVELOPMENT_GATE_DEPLOYMENT_ID + "-")
        or not gate.account_id.startswith(DEVELOPMENT_GATE_ACCOUNT_ID + "-")
        or not _release_covers_development_gate(
            release,
            DEVELOPMENT_GATE_MODULE_IDS,
            registry_allowlist=settings.release_registry_allowlist,
            exact=True,
        )
    ):
        return False
    promotion = store.get_release_promotion(release.version)
    if not is_current_replacement_bootstrap_failure(
        promotion,
        store.list_release_promotion_events(release.version),
        gate_deployment_id=DEVELOPMENT_GATE_DEPLOYMENT_ID,
    ):
        return False
    development_key = getattr(settings, "dev_release_verify_public_key", "")
    if (
        not development_key
        or not promotion.dev_signature
        or not verify_release_signature(
            release_signature_fields(release),
            promotion.dev_signature,
            development_key,
        )
    ):
        return False

    expected_optional_modules = frozenset(DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS)
    expected_versions = {
        module_id: release.modules[module_id]
        for module_id in DEVELOPMENT_GATE_MODULE_IDS
    }
    runs = get_provisioning_run_store().list_runs(
        account_id=gate.account_id,
        deployment_id=gate.id,
    )
    for run in runs:
        payload = run.request_payload or {}
        if (
            run.status == "succeeded"
            and run.external_provider == "hetzner"
            and run.account_id == gate.account_id
            and run.deployment_id == gate.id
            and frozenset(run.module_ids) == expected_optional_modules
            and frozenset(payload.get("module_ids") or ()) == expected_optional_modules
            and payload.get("customer_name") == "One Brain Development Gate"
            and payload.get("account_kind") == "project"
            and payload.get("deployment_type") == "dedicated_server"
            and payload.get("release_ring") == "internal"
            and payload.get("initial_version") == release.version
            and payload.get("current_migration", "") == release.migration_to
            and payload.get("module_versions") == expected_versions
            and payload.get("dry_run") is False
            and (not release.migration_to or run.migration_revision == release.migration_to)
        ):
            return True
    return False


def _development_attempt_note(
    release: ReleaseManifest,
    *,
    ack_restore_required: bool,
    review_note: str,
) -> str:
    note = review_note.strip()
    if release.rollback_kind == "restore_required" and ack_restore_required:
        return f"restore_required acknowledged: {note}"[:1000]
    return note[:1000]


def _fail_development_preflight(
    store,
    promotion: ReleasePromotion,
    *,
    version: str,
    gate_deployment_id: str,
    actor: str,
    rollout_id: str,
    started_at: str,
    reason: str,
    attempt_note: str = "",
) -> ReleasePromotion:
    promotion = transition_promotion(
        store,
        version,
        to_state="dev_deploying",
        actor=actor,
        action="dev_rollout_started" if promotion.state == "dev_pending" else "dev_rollout_retried",
        note=attempt_note,
        fields={
            "gate_deployment_id": gate_deployment_id,
            "dev_rollout_id": "",
            "dev_attempt_id": rollout_id,
            "dev_started_at": started_at,
            "dev_completed_at": "",
            "dev_verified_at": "",
            "failure_reason": "",
        },
    )
    return transition_promotion(
        store,
        version,
        to_state="dev_failed",
        actor="mission-control",
        action="dev_preflight_failed",
        note=reason,
        fields={"failure_reason": "dev_preflight_failed"},
    )


def _dispatch_development_candidate(
    store,
    version: str,
    *,
    actor: str,
    ack_restore_required: bool = False,
    review_note: str = "",
) -> ReleasePromotion:
    promotion = store.get_release_promotion(version)
    if not promotion or promotion.state not in {"dev_pending", "dev_failed"}:
        if not promotion:
            raise ValueError(f"unknown release promotion: {version}")
        return promotion
    gate = store.get_release_gate()
    if not gate:
        return promotion
    release = store.get_release(version)
    if not release:
        raise ValueError(f"unknown release candidate: {version}")
    note = review_note.strip()
    acknowledged = bool(
        ack_restore_required and release.rollback_kind == "restore_required"
    )
    if acknowledged and not note:
        raise ValueError("restore_required_review_note_required")
    attempt_note = _development_attempt_note(
        release,
        ack_restore_required=acknowledged,
        review_note=note,
    )
    if store.list_active_rollout(gate.id) or any(
        queued.state == "dev_deploying"
        and queued.gate_deployment_id == gate.id
        and queued.release_version != version
        for queued in store.list_release_promotions()
    ):
        # A successful rollout still needs its exact post-deploy heartbeat before
        # another candidate may use the gate. Keep later builds queued rather than
        # turning ordinary CI concurrency into a false development failure.
        return promotion
    try:
        heartbeat_at = datetime.fromisoformat(gate.last_heartbeat_at)
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        report_seconds = int(getattr(get_settings(), "fleet_report_seconds", 300) or 300)
        fresh = (datetime.now(timezone.utc) - heartbeat_at).total_seconds() <= max(
            600, report_seconds * 2
        )
    except (AttributeError, TypeError, ValueError):
        fresh = False
    if gate.last_heartbeat_healthy is not True or not fresh:
        return promotion
    eligibility = _resolve_pull_target(gate.id)
    if not eligibility.allowed and eligibility.reason == SECRETS_EPOCH_PENDING_REASON:
        # The host is healthy and enrolled but still applying a just-rotated
        # encrypted bundle. Keep the candidate queued; the next healthy heartbeat
        # calls dispatch_waiting_development_candidate and retries eligibility.
        return promotion
    rollout_id = f"roll_dev_{uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc).isoformat()
    current_module_ids = {
        module.module_id
        for module in store.list_modules(gate.id)
        if module.status == "active"
    }
    module_reason = validate_module_transition(current_module_ids, release.modules)
    if module_reason:
        return _fail_development_preflight(
            store,
            promotion,
            version=version,
            gate_deployment_id=gate.id,
            actor=actor,
            rollout_id=rollout_id,
            started_at=started_at,
            reason=module_reason,
            attempt_note=attempt_note,
        )
    if not eligibility.allowed:
        return _fail_development_preflight(
            store,
            promotion,
            version=version,
            gate_deployment_id=gate.id,
            actor=actor,
            rollout_id=rollout_id,
            started_at=started_at,
            reason=eligibility.reason or "development_gate_target_unavailable",
            attempt_note=attempt_note,
        )
    # Candidate delivery is gated even during the report-only rollout phase. A
    # warning here is a real dev-readiness failure, not permission to proceed.
    plan = store.plan_update(
        gate.id,
        version,
        ack_restore_required=acknowledged,
    )
    if not plan.allowed or plan.warnings:
        return _fail_development_preflight(
            store,
            promotion,
            version=version,
            gate_deployment_id=gate.id,
            actor=actor,
            rollout_id=rollout_id,
            started_at=started_at,
            reason=plan.reason if not plan.allowed else ",".join(plan.warnings),
            attempt_note=attempt_note,
        )
    # Postgres enforces promotion.dev_rollout_id -> rollouts.id. Persist the
    # rollout before attaching it to the promotion, then let the dispatcher use
    # that exact row instead of attempting to create it a second time.
    try:
        store.start_rollout(RolloutRun(
            id=rollout_id,
            deployment_id=gate.id,
            target_version=version,
            status="pending",
            started_by=f"release-candidate:{actor}",
            notes=note,
            ack_restore_required=acknowledged,
        ))
    except ValueError:
        return promotion
    promotion = transition_promotion(
        store,
        version,
        to_state="dev_deploying",
        actor=actor,
        action="dev_rollout_started" if promotion.state == "dev_pending" else "dev_rollout_retried",
        note=attempt_note,
        fields={
            "gate_deployment_id": gate.id,
            "dev_rollout_id": rollout_id,
            "dev_attempt_id": rollout_id,
            "dev_started_at": started_at,
            "dev_completed_at": "",
            "dev_verified_at": "",
            "failure_reason": "",
        },
    )
    settings = get_settings()
    callback_url = (
        f"{settings.fleet_public_url.rstrip('/')}/api/rollouts/{{rollout_id}}/callback"
        if settings.fleet_public_url else ""
    )
    _dispatch_child_rollout(
        "",
        gate.id,
        target_version=version,
        callback_url=callback_url,
        dry_run=False,
        child_id=rollout_id,
        started_by=f"release-candidate:{actor}",
        child_precreated=True,
    )
    rollout = store.get_rollout(rollout_id)
    if not rollout or rollout.exec_status in {"dispatch_failed", "failed"}:
        return transition_promotion(
            store,
            version,
            to_state="dev_failed",
            actor="mission-control",
            action="dev_dispatch_failed",
            note="development rollout could not be dispatched",
            fields={
                "dev_completed_at": datetime.now(timezone.utc).isoformat(),
                "failure_reason": "dev_dispatch_failed",
            },
        )
    return promotion


def dispatch_waiting_development_candidate(store, *, actor: str = "mission-control") -> ReleasePromotion | None:
    gate = store.get_release_gate()
    if not gate or store.list_active_rollout(gate.id):
        return None
    pending = [
        promotion for promotion in store.list_release_promotions()
        if promotion.state == "dev_pending"
    ]
    pending.sort(key=lambda promotion: (promotion.created_at, promotion.release_version))
    return _dispatch_development_candidate(store, pending[0].release_version, actor=actor) if pending else None


def _newest_operator_self_target(store) -> ReleaseManifest | None:
    """The highest-version release the development gate has VERIFIED (dev_verified or
    later, never yanked) — the release Mission Control's own box should be running."""
    best: ReleaseManifest | None = None
    for promotion in store.list_release_promotions():
        if promotion.state not in {"dev_verified", "customer_approved"}:
            continue
        release = store.get_release(promotion.release_version)
        if release is None or release.status == "yanked":
            continue
        if best is None:
            best = release
            continue
        comparison = compare_versions(release.version, best.version)
        if comparison is not None and comparison > 0:
            best = release
    return best


def dispatch_operator_self_rollout(store, settings, *, actor: str = "mission-control") -> RolloutRun | None:
    """Green main -> Mission Control. When operator self-deploy is enabled, open ONE
    pull rollout that moves MC's OWN box to the newest development-VERIFIED release, so a
    merged, gate-verified change reaches the control plane without an operator hand-
    signing and hand-deploying it. Deliberately conservative and idempotent:
      * no-op unless is_operator_self_deployment() holds for MC's own deployment row;
      * one self-update at a time (skips while any rollout is active);
      * only rolls FORWARD (target strictly newer than current_version);
      * never re-attempts a target that already failed — a newer release supersedes it,
        so a genuinely bad build cannot hot-loop the control plane;
      * defers to the shared plan gate (operator_self path): a migration-crossing
        release, for instance, still needs a fresh backup before it can proceed.
    Customer delivery is untouched: this only ever targets MC's own deployment id, and
    only with the CI development signature it already trusts for its own box."""
    if not getattr(settings, "operator_auto_deploy_enabled", False):
        return None
    deployment_id = (getattr(settings, "deployment_id", "") or "").strip()
    if not deployment_id:
        return None
    deployment = store.get_deployment(deployment_id)
    if deployment is None or not is_operator_self_deployment(deployment, settings):
        return None
    if store.list_active_rollout(deployment_id):
        return None
    candidate = _newest_operator_self_target(store)
    if candidate is None:
        return None
    if deployment.current_version:
        comparison = compare_versions(candidate.version, deployment.current_version)
        if comparison is None or comparison <= 0:
            return None  # already on the tip, or an incomparable version — never roll
    if any(
        rollout.target_version == candidate.version and rollout.status == "failed"
        for rollout in store.list_rollouts(deployment_id)
    ):
        return None  # a prior attempt at this exact target failed; wait for a newer release
    if not store.plan_update(deployment_id, candidate.version).allowed:
        return None
    rollout = store.start_rollout(RolloutRun(
        id=f"roll_mc_{uuid4().hex[:12]}",
        deployment_id=deployment_id,
        target_version=candidate.version,
        status="pending",
        started_by=f"operator-self:{actor}",
        notes="operator self-deploy: development-verified tip",
    ))
    # Offer it through the SAME signed desired-state pull path every Hetzner box uses.
    # MC converges on its own envelope; the reconcile tick resolves the terminal state
    # from MC's self-heartbeat (version + migration + health).
    offer_pull_target(store, rollout, target_source="operator_self")
    return rollout


@router.post("/release-candidates", response_model=ReleaseCandidateOut)
def release_candidate(
    body: ReleaseCandidateRequest,
    authorization: str = Header(default=""),
    x_onebrain_candidate_key_id: str = Header(default=""),
):
    actor = _require_candidate_auth(authorization, x_onebrain_candidate_key_id)
    settings = get_settings()
    store = get_control_plane_store()
    if body.action == "prepare":
        try:
            release = prepare_candidate(
                version=body.version,
                git_sha=body.git_sha,
                changed_modules=body.modules,
                changed_images=body.images,
                baseline=_latest_approved_release(store),
                migration_from=body.migration_from,
                migration_to=body.migration_to,
                rollback_kind=body.rollback_kind,
                security_notes=body.security_notes,
                rollback_plan=body.rollback_plan,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ReleaseCandidateOut(
            release=_release_out(release),
            manifest_digest=manifest_digest(release),
            dispatch_state="prepared",
        )

    release = ReleaseManifest(
        version=body.version.strip(),
        git_sha=body.git_sha.strip(),
        modules={key.strip(): value.strip() for key, value in body.modules.items()},
        migration_from=body.migration_from.strip(),
        migration_to=body.migration_to.strip(),
        security_notes=body.security_notes.strip(),
        rollback_plan=body.rollback_plan.strip(),
        status="draft",
        images={key.strip(): value.strip() for key, value in body.images.items()},
        rollback_kind=body.rollback_kind.strip(),
    )
    image_errors = verify_images(
        release.images,
        parse_registry_allowlist(settings.release_registry_allowlist),
    )
    if image_errors:
        raise HTTPException(status_code=400, detail="; ".join(image_errors))
    try:
        promotion, created = register_candidate(
            store,
            release,
            dev_signature=body.dev_signature.strip(),
            dev_signing_key_id=body.dev_signing_key_id.strip(),
            development_public_key=settings.dev_release_verify_public_key,
            production_public_key=settings.release_verify_public_key,
            actor=actor,
        )
        promotion = _dispatch_development_candidate(store, release.version, actor=actor)
    except ValueError as exc:
        detail = str(exc)
        status = 409 if "conflict" in detail or "state" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    stored = store.get_release(release.version) or release
    return ReleaseCandidateOut(
        release=_release_out(stored, promotion, store.list_release_promotion_events(release.version)),
        manifest_digest=manifest_digest(stored),
        created=created,
        dispatch_state=promotion.state,
    )


def _development_gate_blockers(store, gate: CustomerDeployment) -> list[str]:
    blockers: list[str] = []
    if gate.environment != "development" or gate.deployment_type != "dedicated_server":
        blockers.append("development_gate_shape_invalid")
    if gate.status != "active":
        blockers.append("development_gate_inactive")
    if not any(key.status == "active" for key in get_fleet_store().list_keys(gate.id)):
        blockers.append("development_gate_not_enrolled")
    try:
        heartbeat_at = datetime.fromisoformat(gate.last_heartbeat_at)
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - heartbeat_at).total_seconds() > max(
            600, get_settings().fleet_report_seconds * 2
        ):
            blockers.append("deployment_heartbeat_stale")
    except (TypeError, ValueError):
        blockers.append("deployment_heartbeat_stale")
    if gate.last_heartbeat_healthy is not True:
        blockers.append("deployment_unhealthy")
    from app.provisioning.bundles import resolve_module_composition

    required_modules = set(resolve_module_composition(DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS).modules)
    installed_modules = {
        module.module_id: module.version
        for module in store.list_modules(gate.id)
        if module.status == "active"
    }
    if set(installed_modules) != required_modules:
        blockers.append("development_gate_module_set_invalid")
    release = store.get_release(gate.current_version) if gate.current_version else None
    settings = get_settings()
    production_key = getattr(settings, "release_verify_public_key", "")
    production_baseline_trusted = bool(
        release
        and release.status == "active"
        and release.signature
        and production_key
        and verify_release_signature(
            release_signature_fields(release),
            release.signature,
            production_key,
        )
    )
    replacement_seed_trusted = False
    if not production_baseline_trusted:
        replacement_seed_trusted = _replacement_development_gate_seed_is_trusted(
            store,
            gate,
            release,
            settings=settings,
        )
    if (
        not (production_baseline_trusted or replacement_seed_trusted)
        or gate.last_reported_version != release.version
        or (release.migration_to and gate.last_reported_migration != release.migration_to)
    ):
        blockers.append("development_baseline_untrusted")
    elif any(installed_modules.get(module_id) != release.modules.get(module_id)
             for module_id in required_modules):
        blockers.append("development_gate_module_version_invalid")
    return list(dict.fromkeys(blockers))


# Provisioning-run states that mark a gate attempt dead. A dispatch/callback
# failure (including a broker fleet-cap rejection) leaves the CustomerDeployment
# row "active" and moves only its ProvisioningRun terminal, so the newest run --
# not deployment.status -- is the authoritative liveness signal. This is
# app.provisioning.runs.TERMINAL_STATUSES minus the success state.
_DEAD_PROVISION_RUN_STATUSES = frozenset({"failed", "dispatch_failed", "cancelled"})


def _is_live_gate_replacement(deployment, current, newest_run_status: str) -> bool:
    """Whether a gate-identity row is a live replacement competing for the slot.

    The provisioning guard exists to stop a second billed box being created while
    a replacement is genuinely in flight. It must not count rows that can never
    become the gate: there is no API to delete a deployment, so a dead row would
    otherwise wedge gate provisioning permanently. A row is NOT live when it is:

    - the current designated gate itself;
    - the bare, unsuffixed base id while a suffixed gate is already designated --
      replacements always take a '<base>-<suffix>' id, so a lingering bare-base
      row beside a designated gate is a pre-replacement legacy artifact, not an
      in-flight provision;
    - a dead provision attempt. The row is created "active" and its outcome lands
      on the ProvisioningRun, not the deployment status -- a dispatch/callback
      failure leaves the row "active" -- so a terminally failed newest run marks
      it dead. A row that left "active" by any other path is dead too.
    """
    if current is not None and deployment.id == current.id:
        return False
    if current is not None and deployment.id == DEVELOPMENT_GATE_DEPLOYMENT_ID:
        return False
    if deployment.status != "active":
        return False
    if newest_run_status in _DEAD_PROVISION_RUN_STATUSES:
        return False
    return True


def _development_gate_identity(store, run_store) -> tuple[str, str]:
    """Return a fixed first gate identity or a server-generated replacement one.

    A second *live* undesignated gate is refused rather than creating another
    billed server on retries. Replacements use a unique deployment ID, which
    gives the Hetzner renderer a distinct compose project and DNS label beside
    the old gate. Dead rows -- a failed attempt (its newest provisioning run is
    terminally failed), or a superseded legacy gate the release-gate marker has
    since moved off of -- are ignored so they cannot wedge provisioning: there is
    no delete API to clear them by hand.
    """
    current = store.get_release_gate()

    def _newest_run_status(deployment_id: str) -> str:
        runs = run_store.list_runs(deployment_id=deployment_id)
        return runs[0].status if runs else ""

    live = [
        deployment for deployment in store.list_deployments()
        if (
            deployment.id == DEVELOPMENT_GATE_DEPLOYMENT_ID
            or deployment.id.startswith(DEVELOPMENT_GATE_DEPLOYMENT_ID + "-")
        )
        and _is_live_gate_replacement(deployment, current, _newest_run_status(deployment.id))
    ]
    if live:
        raise ValueError("A development gate replacement already exists.")
    if not current:
        return DEVELOPMENT_GATE_DEPLOYMENT_ID, DEVELOPMENT_GATE_ACCOUNT_ID
    for _ in range(3):
        suffix = uuid4().hex[:12]
        deployment_id = f"{DEVELOPMENT_GATE_DEPLOYMENT_ID}-{suffix}"
        if not store.get_deployment(deployment_id):
            return deployment_id, f"{DEVELOPMENT_GATE_ACCOUNT_ID}-{suffix}"
    raise ValueError("Could not allocate a unique development gate replacement identity.")


def _development_gate_out(store) -> DevelopmentGateOut:
    gate = store.get_release_gate()
    if not gate:
        return DevelopmentGateOut(blockers=["development_gate_missing"])
    blockers = _development_gate_blockers(store, gate)
    return DevelopmentGateOut(
        deployment=_deployment_out(gate),
        ready=not blockers,
        blockers=blockers,
    )


@router.get("/development-gate", response_model=DevelopmentGateOut)
def get_development_gate(principal: Principal = Depends(resolve_principal)):
    _require_admin(principal)
    _require_operator_mode()
    return _development_gate_out(get_control_plane_store())


@router.post(
    "/development-gate/prepare-existing",
    response_model=DevelopmentGatePreparationOut,
)
def prepare_existing_development_gate(
    principal: Principal = Depends(resolve_principal),
):
    """Prepare the designated enrolled gate in place; never provision a server."""
    _require_admin(principal)
    _require_operator_mode()
    settings = get_settings()
    if not active_signer_in_served_set(settings):
        raise HTTPException(
            status_code=409,
            detail="Active desired-state signer is not in the served public-key set.",
        )

    store = get_control_plane_store()
    gate = store.get_release_gate()
    if gate is None:
        raise HTTPException(status_code=409, detail="No development gate is designated.")
    if (
        gate.environment != "development"
        or gate.deployment_type != "dedicated_server"
        or gate.status != "active"
    ):
        raise HTTPException(status_code=409, detail="Designated development gate shape is invalid.")

    fleet = get_fleet_store()
    if not any(key.status == "active" for key in fleet.list_keys(gate.id)):
        raise HTTPException(status_code=409, detail="Development gate has no active fleet key.")
    heartbeat = fleet.latest_heartbeat(gate.id)
    if heartbeat is None or heartbeat.deployment_id != gate.id or heartbeat.healthy is not True:
        raise HTTPException(status_code=409, detail="Development gate heartbeat is missing or unhealthy.")
    try:
        received_at = datetime.fromisoformat(heartbeat.received_at)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        fleet_report_seconds = int(getattr(settings, "fleet_report_seconds", 300) or 300)
        if (datetime.now(timezone.utc) - received_at).total_seconds() > max(
            600, fleet_report_seconds * 2
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Development gate heartbeat is stale.")

    from app.provisioning.gate_adoption import (
        prepare_existing_gate_bundle,
        retire_superseded_gate_keys,
    )

    try:
        result = prepare_existing_gate_bundle(
            deployment=gate,
            provision_store=get_provisioning_run_store(),
            service_key_store=get_service_key_store(),
            settings=settings,
            optional_module_ids=DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Development gate preparation failed safely.") from exc

    update = heartbeat.payload.get("update", {}) if isinstance(heartbeat.payload, dict) else {}
    try:
        applied_epoch = int(update.get("applied_secrets_epoch", 0) or 0)
    except (TypeError, ValueError):
        applied_epoch = 0
    ready = applied_epoch >= result.secrets_epoch
    if ready:
        try:
            retire_superseded_gate_keys(
                deployment=gate,
                provision_store=get_provisioning_run_store(),
                service_key_store=get_service_key_store(),
                settings=settings,
                optional_module_ids=DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Development gate key retirement failed safely.") from exc
    return DevelopmentGatePreparationOut(
        deployment_id=gate.id,
        updated=result.updated,
        secrets_epoch=result.secrets_epoch,
        applied_secrets_epoch=applied_epoch,
        ready=ready,
        blockers=[] if ready else ["secrets_epoch_pending"],
    )


@router.put("/development-gate/{deployment_id}", response_model=DevelopmentGateOut)
def designate_development_gate(
    deployment_id: str,
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    _require_operator_mode()
    store = get_control_plane_store()
    candidate = store.get_deployment(deployment_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Deployment not found.")
    blockers = _development_gate_blockers(store, candidate)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail=f"Development gate is not ready: {','.join(blockers)}",
        )
    try:
        store.designate_release_gate(deployment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    dispatch_waiting_development_candidate(store, actor=principal.user_id)
    return _development_gate_out(store)


@router.post("/development-gate/provision")
def provision_development_gate(
    body: DevelopmentGateProvisionIn,
    principal: Principal = Depends(resolve_principal),
):
    """Create the Core-plus-modules development gate; designation waits for verification."""
    _require_admin(principal)
    _require_operator_mode()
    settings = get_settings()
    if settings.provisioner_backend != "hetzner":
        raise HTTPException(status_code=409, detail="Development gate provisioning requires the Hetzner backend.")
    store = get_control_plane_store()
    try:
        deployment_id, account_id = _development_gate_identity(store, get_provisioning_run_store())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from app.provisioning.bundles import resolve_module_composition
    from app.routers.provisioning import CustomerProvisionCreate, _provision_customer_impl

    composition = resolve_module_composition(DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS)
    deployable_module_ids = composition.modules
    baseline, baseline_source = _development_gate_provisioning_baseline(
        store,
        deployable_module_ids,
        settings=settings,
    )
    if not baseline:
        raise HTTPException(status_code=409, detail="A trusted approved baseline release is required first.")
    missing = sorted(module_id for module_id in deployable_module_ids if module_id not in baseline.modules)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Baseline release does not cover the development gate modules: {','.join(missing)}",
        )
    missing_images = sorted(module_id for module_id in deployable_module_ids if module_id not in baseline.images)
    image_errors = verify_images(
        {module_id: baseline.images[module_id] for module_id in deployable_module_ids if module_id in baseline.images},
        parse_registry_allowlist(settings.release_registry_allowlist),
    )
    if missing_images or image_errors:
        detail = [f"missing images: {','.join(missing_images)}"] if missing_images else []
        detail.extend(image_errors)
        raise HTTPException(
            status_code=409,
            detail=f"Trusted baseline is not deployable: {'; '.join(detail)}",
        )
    if body.dry_run:
        return {
            "dry_run": True,
            "deployment": {"id": deployment_id},
            "account_id": account_id,
            "module_ids": list(composition.selected_module_ids),
            "environment": "development",
            "deployment_type": "dedicated_server",
            "region": body.region.strip(),
            "baseline_source": baseline_source,
            "initial_version": baseline.version,
            "modules": {module_id: baseline.modules[module_id] for module_id in deployable_module_ids},
            "images": {module_id: baseline.images[module_id] for module_id in deployable_module_ids},
        }
    if not settings.dev_release_verify_public_key:
        raise HTTPException(
            status_code=409,
            detail="Mission Control development release verification key is required.",
        )
    callback_url = (
        f"{settings.fleet_public_url.rstrip('/')}/api/provisioning/runs/{{run_id}}/callback"
        if settings.fleet_public_url else ""
    )
    if not callback_url:
        raise HTTPException(status_code=409, detail="Mission Control fleet_public_url is required.")
    return _provision_customer_impl(CustomerProvisionCreate(
        customer_name="One Brain Development Gate",
        module_ids=list(composition.selected_module_ids),
        account_kind="project",
        account_id=account_id,
        deployment_id=deployment_id,
        owner_email=body.owner_email.strip(),
        deployment_type="dedicated_server",
        environment="development",
        region=body.region.strip(),
        release_ring="internal",
        initial_version=baseline.version,
        current_migration=baseline.migration_to,
        module_versions={module_id: baseline.modules[module_id] for module_id in deployable_module_ids},
        mint_integration_keys=True,
        external_provisioning=True,
        dry_run=body.dry_run,
        callback_url=callback_url,
    ), principal)


@router.post("/releases/{version}/retry-dev", response_model=ReleaseOut)
def retry_development_release(
    version: str,
    body: DevelopmentRetryIn,
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    _require_operator_mode()
    store = get_control_plane_store()
    promotion = store.get_release_promotion(version)
    if not promotion or promotion.state != "dev_failed":
        raise HTTPException(status_code=409, detail="Only a failed development candidate can be retried.")
    try:
        promotion = _dispatch_development_candidate(
            store,
            version,
            actor=principal.user_id,
            ack_restore_required=body.ack_restore_required,
            review_note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    release = store.get_release(version)
    return _release_out(release, promotion, store.list_release_promotion_events(version))


@router.post("/releases/{version}/production-signature", response_model=ReleaseOut)
def upload_production_signature(
    version: str,
    body: ProductionSignatureIn,
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    _require_operator_mode()
    settings = get_settings()
    store = get_control_plane_store()
    try:
        release = attach_production_signature(
            store,
            version,
            signature=body.signature.strip(),
            signing_key_id=body.signing_key_id.strip(),
            production_public_key=settings.release_verify_public_key,
            actor=principal.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    promotion = store.get_release_promotion(version)
    return _release_out(release, promotion, store.list_release_promotion_events(version))


@router.post("/releases/{version}/approve", response_model=ReleaseOut)
def approve_release(
    version: str,
    body: PromotionNote,
    principal: Principal = Depends(resolve_principal),
):
    _require_admin(principal)
    _require_operator_mode()
    store = get_control_plane_store()
    release = store.get_release(version)
    promotion = store.get_release_promotion(version)
    gate = store.get_release_gate()
    if not release or not promotion:
        raise HTTPException(status_code=404, detail="Release candidate not found.")
    if not gate or promotion.gate_deployment_id != gate.id:
        raise HTTPException(status_code=409, detail="Development verification does not match the current gate.")
    try:
        # Re-verify at the approval boundary; validation at upload time is not a
        # substitute for the final safety decision.
        from app.controlplane.promotion import verify_production_signature_match

        verify_production_signature_match(
            release,
            signature=release.signature,
            production_public_key=get_settings().release_verify_public_key,
        )
        promotion = store.approve_release_for_customers(
            version,
            signature=release.signature,
            signing_key_id=release.signing_key_id,
            actor=principal.user_id,
            note=body.note.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _release_out(
        store.get_release(version), promotion, store.list_release_promotion_events(version)
    )


def _promotion_action(
    version: str,
    *,
    to_state: str,
    action: str,
    note: str,
    principal: Principal,
) -> ReleaseOut:
    _require_admin(principal)
    _require_operator_mode()
    store = get_control_plane_store()
    now = datetime.now(timezone.utc).isoformat()
    fields: dict = {}
    if to_state == "customer_paused":
        fields = {
            "customer_paused_at": now,
            "customer_paused_reason": note.strip() or "operator_pause",
            "failure_reason": note.strip() or "operator_pause",
        }
    elif to_state == "customer_approved":
        if not note.strip():
            raise HTTPException(status_code=400, detail="A review note is required to resume customer delivery.")
        fields = {"customer_paused_reason": "", "failure_reason": ""}
    elif to_state == "yanked":
        fields = {"yanked_at": now, "failure_reason": note.strip() or "operator_yank"}
    try:
        promotion = transition_promotion(
            store,
            version,
            to_state=to_state,
            actor=principal.user_id,
            action=action,
            note=note.strip(),
            fields=fields,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _release_out(
        store.get_release(version), promotion, store.list_release_promotion_events(version)
    )


@router.post("/releases/{version}/pause", response_model=ReleaseOut)
def pause_release(version: str, body: PromotionNote, principal: Principal = Depends(resolve_principal)):
    return _promotion_action(
        version, to_state="customer_paused", action="customer_delivery_paused",
        note=body.note, principal=principal,
    )


@router.post("/releases/{version}/resume", response_model=ReleaseOut)
def resume_release(version: str, body: PromotionNote, principal: Principal = Depends(resolve_principal)):
    return _promotion_action(
        version, to_state="customer_approved", action="customer_delivery_resumed",
        note=body.note, principal=principal,
    )


@router.post("/releases/{version}/yank", response_model=ReleaseOut)
def yank_release(version: str, body: PromotionNote, principal: Principal = Depends(resolve_principal)):
    return _promotion_action(
        version, to_state="yanked", action="release_yanked", note=body.note, principal=principal,
    )


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


def offer_pull_target(
    control,
    rollout,
    *,
    target_source: str,
    required_secrets_epoch: int = 0,
) -> None:
    """Offer a rollout to a Hetzner box through its signed desired state.

    The box reports the outcome through its fleet report; the reconcile tick
    synthesizes the terminal state. No external workflow is dispatched.
    """
    if not control.claim_rollout_dispatch(rollout.id):
        return
    control.update_rollout_exec(rollout.id, dispatched_at=datetime.now(timezone.utc).isoformat(),
                                request_payload={
                                    "provider": "hetzner",
                                    "pull": True,
                                    "target_source": target_source,
                                    "required_secrets_epoch": max(
                                        0, int(required_secrets_epoch or 0)
                                    ),
                                })


def _resolve_pull_target(deployment_id: str):
    settings = get_settings()
    return resolve_pull_target(
        get_provisioning_run_store(),
        get_control_plane_store(),
        get_fleet_store(),
        deployment_id,
        heartbeat_max_age_seconds=max(
            600,
            int(getattr(settings, "fleet_report_seconds", 300) or 300) * 2,
        ),
    )


@router.post("/deployments/{deployment_id}/rollouts/{rollout_id}/dispatch", response_model=RolloutOut)
def dispatch_rollout(
    deployment_id: str,
    rollout_id: str,
    body: RolloutDispatch,
    principal: Principal = Depends(resolve_principal),
):
    """Offer an existing rollout to a Hetzner deployment (fail closed otherwise)."""
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
                               ack_restore_required=rollout.ack_restore_required,
                               ignore_rollout_id=rollout.id)
    if not plan.allowed:
        raise HTTPException(status_code=409, detail=f"Update blocked: {plan.reason}")
    release = control.get_release(rollout.target_version)
    deployment = control.get_deployment(deployment_id)
    if not release or not deployment:
        raise HTTPException(status_code=409, detail="Rollout target is no longer available.")

    _validate_callback_url(body.callback_url, placeholder="{rollout_id}")
    eligibility = _resolve_pull_target(deployment_id)
    if eligibility.allowed:
        # The box converges on its own signed desired state and the reconcile
        # tick resolves the terminal state from its fleet report.
        offer_pull_target(
            control,
            rollout,
            target_source=eligibility.source,
            required_secrets_epoch=getattr(eligibility, "required_secrets_epoch", 0),
        )
        return _rollout_out(control.get_rollout(rollout_id))

    reason = eligibility.reason or "Rollout target is not a Hetzner deployment."
    mark_rollout_dispatch_failed(control, rollout, reason)
    raise HTTPException(status_code=409, detail=reason)


# --- fleet rollouts (Phase 2: ring-by-ring fleet-wide update) ----------------

class FleetRolloutCreate(BaseModel):
    target_version: str = Field(min_length=1, max_length=120)
    callback_url: str = Field(min_length=1, max_length=500)
    failure_tolerance: int = Field(default=0, ge=0, le=10000)
    dry_run: bool = True
    # P4-07 targeting (all defaults reproduce today's whole-fleet, whole-ring sweep):
    deployment_ids: list[str] = Field(default_factory=list, max_length=1000)  # named set ("" -> all eligible)
    include_manual_pinned: bool = False       # override manual/pinned policy for NAMED deployments
    ring_batch_size: int = Field(default=1, ge=0, le=10000)  # customer safety default: one at a time


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
    updated_at: str = ""
    ring_batch_size: int = 1
    deployment_ids: list[str] = Field(default_factory=list)
    include_manual_pinned: bool = False


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
        started_by=fr.started_by, notes=fr.notes, created_at=fr.created_at, updated_at=fr.updated_at,
        ring_batch_size=fr.ring_batch_size,
        deployment_ids=list(fr.only_deployment_ids),
        include_manual_pinned=fr.include_manual_pinned,
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
                            callback_url: str, dry_run: bool, child_id: str = "",
                            started_by: str = "", child_precreated: bool = False) -> None:
    """Create and dispatch ONE child rollout for a fleet ring. A dispatch failure is
    recorded on the child (dispatch_failed, i.e. bookkeeping 'failed') so the fleet
    reducer counts it toward failure_tolerance; this never raises."""
    control = get_control_plane_store()
    child_id = child_id or f"roll_{uuid4().hex[:12]}"
    if not child_precreated:
        try:
            control.start_rollout(RolloutRun(
                id=child_id, deployment_id=deployment_id, target_version=target_version,
                status="pending", started_by=started_by or f"fleet:{fleet_id}",
                fleet_rollout_id=fleet_id))
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
                               ack_restore_required=rollout.ack_restore_required,
                               ignore_rollout_id=rollout.id)
    if not (release and deployment and plan.allowed):
        mark_rollout_dispatch_failed(control, rollout, "update no longer available")
        return
    eligibility = _resolve_pull_target(deployment_id)
    if eligibility.allowed:
        # The child remains in-flight until the box reports a terminal state.
        offer_pull_target(
            control,
            rollout,
            target_source=eligibility.source,
            required_secrets_epoch=getattr(eligibility, "required_secrets_epoch", 0),
        )
        return
    mark_rollout_dispatch_failed(
        control,
        rollout,
        eligibility.reason or "Rollout target is not a Hetzner deployment.",
    )


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
    _validate_callback_url(body.callback_url, placeholder="{rollout_id}")
    control = get_control_plane_store()
    release = control.get_release(body.target_version)
    if not release:
        raise HTTPException(status_code=404, detail="No such release.")
    if getattr(get_settings(), "release_promotion_required", False):
        if body.failure_tolerance != 0:
            raise HTTPException(status_code=400, detail="Promoted customer rollouts require failure_tolerance=0.")
        if body.ring_batch_size != 1:
            raise HTTPException(status_code=400, detail="Promoted customer rollouts run one deployment at a time.")
        if not body.deployment_ids:
            raise HTTPException(status_code=400, detail="Promoted customer rollouts require explicit deployment_ids.")

    fleet_run, plan = plan_and_start_fleet_rollout(
        control, control, fleet_id=f"fleet_{uuid4().hex[:12]}", target_version=body.target_version,
        git_sha=release.git_sha, failure_tolerance=body.failure_tolerance,
        started_by=principal.user_id, created_at=datetime.now(timezone.utc).isoformat(),
        callback_url=body.callback_url, dry_run=body.dry_run, dispatch_child=fleet_dispatch_child,
        ring_batch_size=body.ring_batch_size, only_deployment_ids=frozenset(body.deployment_ids),
        include_manual_pinned=body.include_manual_pinned)
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
    runs = reconcile_once(get_settings(), control, get_fleet_store())
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

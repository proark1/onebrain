"""Control-plane records and store contract.

This domain tracks customer deployments and release state. It deliberately
stores deployment metadata only, never customer content.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol


MODULE_IDS = frozenset({
    "onebrain-api",
    "onebrain-admin-ui",
    "onebrain-workers",
    "assistant-service",
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
})
ROLL_OUT_RINGS = frozenset({"internal", "pilot", "early", "stable", "manual"})
# Existing rows may contain these retired provider values.  Keep them readable
# until a separately approved data migration can retire historic records; all
# creation paths use ACTIVE_DEPLOYMENT_TYPES below.
ACTIVE_DEPLOYMENT_TYPES = frozenset({"dedicated_server", "customer_owned"})
RETIRED_DEPLOYMENT_TYPES = frozenset({"dedicated_railway", "shared_railway"})
DEPLOYMENT_TYPES = ACTIVE_DEPLOYMENT_TYPES | RETIRED_DEPLOYMENT_TYPES
RUN_STATUSES = frozenset({"pending", "running", "success", "failed", "paused"})
PROMOTION_STATES = frozenset({
    "dev_pending",
    "dev_deploying",
    "dev_verified",
    "dev_failed",
    "customer_approved",
    "customer_paused",
    "yanked",
})

ROLLBACK_KINDS = frozenset({"", "code_only", "restore_required"})
UPDATE_POLICIES = frozenset({"", "auto", "manual", "pinned"})
TEARDOWN_REQUEST_PENDING = "pending"
TEARDOWN_REQUEST_EXECUTION_DISABLED = "execution_disabled"
TEARDOWN_REQUEST_EXPIRED = "expired"
TEARDOWN_REQUEST_STATUSES = frozenset({
    TEARDOWN_REQUEST_PENDING,
    TEARDOWN_REQUEST_EXECUTION_DISABLED,
    TEARDOWN_REQUEST_EXPIRED,
})
TEARDOWN_EXECUTION_DISABLED_RESULT = "execution_disabled: no customer resources were deleted"
# registry/repo@sha256:<64 hex> — digest-pinned image reference, never a floating tag.
IMAGE_DIGEST_RE = re.compile(
    r"^(?P<registry>[a-z0-9][a-z0-9.\-]*(?::\d+)?)/(?P<repo>[a-z0-9][a-z0-9._\-/]*)@sha256:(?P<digest>[0-9a-f]{64})$"
)


def validate_image_ref(ref: str) -> str | None:
    """Return an error string for a non-digest-pinned image ref, else None."""
    m = IMAGE_DIGEST_RE.match(ref or "")
    if not m:
        return f"image ref is not digest-pinned (registry/repo@sha256:...): {ref!r}"
    registry = m.group("registry")
    if "." not in registry and ":" not in registry:
        return f"image ref has no registry host: {ref!r}"
    return None


def effective_update_policy(deployment) -> str:
    """auto|manual|pinned for any deployment-shaped object (getattr-based so
    SimpleNamespace fakes in orchestration tests keep working). '' update_policy
    falls back to the legacy ring convention: release_ring=='manual' -> manual."""
    policy = getattr(deployment, "update_policy", "") or ""
    if policy:
        return policy
    return "manual" if getattr(deployment, "release_ring", "") == "manual" else "auto"


@dataclass(frozen=True)
class CustomerDeployment:
    id: str
    customer_name: str
    account_id: str = ""          # owning platform account; authoritative for operator authz
    environment: str = "production"
    deployment_type: str = "dedicated_server"
    region: str = ""
    release_ring: str = "manual"
    status: str = "active"
    current_version: str = ""
    current_migration: str = ""
    created_at: str = ""
    update_policy: str = ""       # ''(legacy: derive from ring) | auto | manual | pinned
    is_release_gate: bool = False
    current_version_deployed_at: str = ""
    last_heartbeat_at: str = ""
    last_heartbeat_healthy: Optional[bool] = None
    last_reported_version: str = ""
    last_reported_migration: str = ""
    # Product choices made at provisioning time.  DeploymentModule records are
    # the resolved container services; this preserves selected product modules
    # such as KPI Dashboard and AI Employees that do not add a container today.
    selected_module_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_module_ids", tuple(self.selected_module_ids or ()))


@dataclass(frozen=True)
class DeploymentModule:
    deployment_id: str
    module_id: str
    version: str
    status: str = "active"


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    git_sha: str
    modules: Dict[str, str]
    migration_from: str = ""
    migration_to: str = ""
    security_notes: str = ""
    rollback_plan: str = ""
    status: str = "draft"
    created_at: str = ""
    images: Dict[str, str] = field(default_factory=dict)  # module_id -> registry/repo@sha256:...
    rollback_kind: str = ""       # '' legacy | code_only | restore_required
    signature: str = ""           # base64 Ed25519 over canonical_release_payload (app/trust/release.py)
    signing_key_id: str = ""      # operator-chosen key identifier (rotation metadata; not signed)


@dataclass(frozen=True)
class ReleasePromotion:
    release_version: str
    state: str = "dev_pending"
    gate_deployment_id: str = ""
    dev_signature: str = ""
    dev_signing_key_id: str = ""
    dev_rollout_id: str = ""
    dev_attempt_id: str = ""
    dev_started_at: str = ""
    dev_completed_at: str = ""
    dev_verified_at: str = ""
    customer_approved_at: str = ""
    customer_approved_by: str = ""
    customer_paused_at: str = ""
    customer_paused_reason: str = ""
    yanked_at: str = ""
    failure_reason: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReleasePromotionEvent:
    id: str
    release_version: str
    action: str
    to_state: str
    actor: str = ""
    from_state: str = ""
    note: str = ""
    metadata: Dict = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class BackupRun:
    id: str
    deployment_id: str
    status: str
    detail: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class HealthCheckRun:
    id: str
    deployment_id: str
    status: str
    detail: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class RolloutRun:
    id: str
    deployment_id: str
    target_version: str
    status: str
    started_by: str
    notes: str = ""
    created_at: str = ""
    # Execution lifecycle. Rollouts are offered to Hetzner boxes through the
    # signed desired-state pull path; no workflow dispatcher is involved.
    exec_status: str = "pending"          # see rollout_exec.ROLLOUT_EXEC_STATUSES
    external_provider: str = "hetzner"
    external_run_id: str = ""
    external_run_url: str = ""
    failure_reason: str = ""
    request_payload: Dict = field(default_factory=dict)
    dispatched_at: str = ""
    completed_at: str = ""
    fleet_rollout_id: str = ""            # set when part of a fleet-wide rollout (Phase 2)
    ack_restore_required: bool = False    # explicit human ack for restore_required->auto (D-10)


@dataclass(frozen=True)
class ServedFloorBump:
    """A pre-signed (offline release-key) FloorBump the operator uploaded for MC to
    SERVE (P5-01). MC never signs or mutates it — bump_json is the opaque signed
    FloorBump.model_dump_json(); the box re-verifies the offline signature itself."""
    scope: str                 # '*' (fleet-wide) or a deployment_id
    bump_json: str             # signed FloorBump JSON (opaque to MC)
    floor_version: str = ""    # denormalized for the operator list view
    updated_by: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class CustomerTeardownRequest:
    """A record-only, two-person customer teardown authorization.

    This record deliberately contains no infrastructure identifiers or executor
    instructions.  It can prove review and evidence collection, but it cannot
    perform a remote deletion.
    """
    id: str
    deployment_id: str
    account_id: str
    nonce_hash: str
    nonce_expires_at: str
    legal_hold_evidence_ref: str
    backup_retention_evidence_ref: str
    requested_by: str
    approver_ids: tuple[str, ...] = ()
    status: str = TEARDOWN_REQUEST_PENDING
    execution_result: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


@dataclass(frozen=True)
class UpdatePlan:
    deployment_id: str
    target_version: str
    allowed: bool
    reason: str
    current_modules: Dict[str, str] = field(default_factory=dict)
    target_modules: Dict[str, str] = field(default_factory=dict)
    modules_to_update: Dict[str, str] = field(default_factory=dict)
    rollback_kind: str = ""       # copied from the release when found (operator UI/plan visibility)
    warnings: List[str] = field(default_factory=list)


class ControlPlaneStore(Protocol):
    def create_deployment(self, deployment: CustomerDeployment) -> CustomerDeployment: ...

    def get_deployment(self, deployment_id: str) -> Optional[CustomerDeployment]: ...

    def list_deployments(self) -> List[CustomerDeployment]: ...

    def upsert_module(self, module: DeploymentModule) -> DeploymentModule: ...

    def list_modules(self, deployment_id: str) -> List[DeploymentModule]: ...

    def create_release(self, release: ReleaseManifest) -> ReleaseManifest: ...

    def get_release(self, version: str) -> Optional[ReleaseManifest]: ...

    def list_releases(self) -> List[ReleaseManifest]: ...

    def create_release_candidate(
        self,
        release: ReleaseManifest,
        promotion: ReleasePromotion,
        event: ReleasePromotionEvent,
    ) -> ReleasePromotion: ...

    def get_release_promotion(self, version: str) -> Optional[ReleasePromotion]: ...

    def list_release_promotions(self) -> List[ReleasePromotion]: ...

    def transition_release_promotion(
        self,
        version: str,
        from_states: frozenset[str],
        to_state: str,
        *,
        actor: str,
        action: str,
        note: str = "",
        fields: Optional[Dict] = None,
    ) -> ReleasePromotion: ...

    def list_release_promotion_events(self, version: str) -> List[ReleasePromotionEvent]: ...

    def set_release_production_signature(
        self,
        version: str,
        *,
        signature: str,
        signing_key_id: str,
        actor: str,
    ) -> ReleaseManifest: ...

    def approve_release_for_customers(
        self,
        version: str,
        *,
        signature: str,
        signing_key_id: str,
        actor: str,
        note: str = "",
    ) -> ReleasePromotion: ...

    def get_release_gate(self) -> Optional[CustomerDeployment]: ...

    def designate_release_gate(self, deployment_id: str) -> CustomerDeployment: ...

    def update_deployment_telemetry(
        self,
        deployment_id: str,
        *,
        heartbeat_at: str,
        healthy: bool,
        reported_version: str = "",
        reported_migration: str = "",
    ) -> CustomerDeployment: ...

    def mark_deployment_provisioned(
        self,
        deployment_id: str,
        *,
        installed_at: str,
        version: str,
        migration: str = "",
    ) -> CustomerDeployment: ...

    def record_backup(self, backup: BackupRun) -> BackupRun: ...

    def latest_backup(self, deployment_id: str) -> Optional[BackupRun]: ...

    def record_health(self, health: HealthCheckRun) -> HealthCheckRun: ...

    def latest_health(self, deployment_id: str) -> Optional[HealthCheckRun]: ...

    def plan_update(
        self,
        deployment_id: str,
        target_version: str,
        *,
        ack_restore_required: bool = False,
        ignore_rollout_id: str = "",
    ) -> UpdatePlan: ...

    def set_update_policy(self, deployment_id: str, update_policy: str) -> CustomerDeployment: ...

    def start_rollout(self, rollout: RolloutRun) -> RolloutRun: ...

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "", apply: bool = True) -> RolloutRun: ...

    def get_rollout(self, rollout_id: str) -> Optional[RolloutRun]: ...

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]: ...

    def list_active_rollout(self, deployment_id: str) -> Optional[RolloutRun]: ...

    def list_rollouts_for_fleet(self, fleet_rollout_id: str) -> List[RolloutRun]: ...

    def claim_rollout_dispatch(self, rollout_id: str) -> bool: ...

    def update_rollout_exec(self, rollout_id: str, **fields) -> RolloutRun: ...

    # --- customer teardown protocol (record-only; no execution capability) ---
    def create_teardown_request(self, request: CustomerTeardownRequest) -> CustomerTeardownRequest: ...

    def get_teardown_request(self, request_id: str) -> Optional[CustomerTeardownRequest]: ...

    def list_teardown_requests(self, deployment_id: str) -> List[CustomerTeardownRequest]: ...

    def approve_teardown_request(
        self,
        request_id: str,
        *,
        approver_id: str,
        nonce_hash: str,
        approved_at: str,
    ) -> CustomerTeardownRequest: ...

    # --- served floor bumps (P5-01 revocation kill-switch) ---
    def set_served_floor_bump(self, bump: ServedFloorBump) -> ServedFloorBump: ...

    def clear_served_floor_bump(self, scope: str) -> bool: ...

    def get_served_floor_bump(self, scope: str) -> Optional[ServedFloorBump]: ...

    def list_served_floor_bumps(self) -> List[ServedFloorBump]: ...


def validate_deployment(deployment: CustomerDeployment) -> None:
    if not deployment.id.strip() or not deployment.customer_name.strip():
        raise ValueError("Deployment id and customer name are required.")
    if deployment.deployment_type not in DEPLOYMENT_TYPES:
        raise ValueError(f"Unknown deployment type: {deployment.deployment_type}")
    if deployment.release_ring not in ROLL_OUT_RINGS:
        raise ValueError(f"Unknown release ring: {deployment.release_ring}")
    if deployment.update_policy not in UPDATE_POLICIES:
        raise ValueError(f"Unknown update policy: {deployment.update_policy}")
    module_ids = deployment.selected_module_ids
    if any(not isinstance(module_id, str) or not module_id.strip() for module_id in module_ids):
        raise ValueError("Selected module ids must be non-empty strings.")
    if len(set(module_ids)) != len(module_ids):
        raise ValueError("Selected module ids must not contain duplicates.")


def _parse_teardown_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def validate_teardown_request(request: CustomerTeardownRequest) -> None:
    required = {
        "request id": request.id,
        "deployment id": request.deployment_id,
        "account id": request.account_id,
        "legal-hold evidence reference": request.legal_hold_evidence_ref,
        "backup/retention evidence reference": request.backup_retention_evidence_ref,
        "requester": request.requested_by,
    }
    for label, value in required.items():
        if not str(value).strip():
            raise ValueError(f"{label} is required.")
    if not re.fullmatch(r"[0-9a-f]{64}", request.nonce_hash or ""):
        raise ValueError("teardown request nonce hash must be a SHA-256 hex digest.")
    _parse_teardown_timestamp(request.nonce_expires_at, "nonce expiry")
    if request.status not in TEARDOWN_REQUEST_STATUSES:
        raise ValueError(f"Unknown teardown request status: {request.status}")
    if len(request.approver_ids) > 2 or len(set(request.approver_ids)) != len(request.approver_ids):
        raise ValueError("teardown request approvers must contain at most two distinct identities.")
    if request.requested_by in request.approver_ids:
        raise ValueError("teardown requester cannot approve the request.")
    if any(not approver.strip() for approver in request.approver_ids):
        raise ValueError("teardown approver identity is required.")
    if request.status in {TEARDOWN_REQUEST_EXECUTION_DISABLED, TEARDOWN_REQUEST_EXPIRED}:
        if request.execution_result != TEARDOWN_EXECUTION_DISABLED_RESULT:
            raise ValueError("terminal teardown requests require an execution-disabled result.")
    if request.status == TEARDOWN_REQUEST_EXECUTION_DISABLED and len(request.approver_ids) != 2:
        raise ValueError("execution-disabled teardown requests require two approvals.")


def apply_teardown_approval(
    request: CustomerTeardownRequest,
    *,
    approver_id: str,
    nonce_hash: str,
    approved_at: str,
) -> CustomerTeardownRequest:
    """Apply one approval without ever exposing or handling a raw nonce.

    An expired request becomes terminal with the same explicit non-execution
    result.  Callers audit and surface that as a denial rather than treating it
    as a successful approval.
    """
    validate_teardown_request(request)
    actor = approver_id.strip()
    if not actor:
        raise ValueError("teardown approver identity is required.")
    if request.status != TEARDOWN_REQUEST_PENDING:
        raise ValueError("teardown request is no longer pending.")
    now = _parse_teardown_timestamp(approved_at, "approval time")
    if _parse_teardown_timestamp(request.nonce_expires_at, "nonce expiry") <= now:
        return replace(
            request,
            status=TEARDOWN_REQUEST_EXPIRED,
            execution_result=TEARDOWN_EXECUTION_DISABLED_RESULT,
            updated_at=approved_at,
            completed_at=approved_at,
        )
    if not nonce_hash or not hmac.compare_digest(request.nonce_hash, nonce_hash):
        raise ValueError("teardown approval nonce is invalid.")
    if actor == request.requested_by:
        raise ValueError("teardown requester cannot approve the request.")
    if actor in request.approver_ids:
        raise ValueError("teardown approver has already approved this request.")
    approvers = (*request.approver_ids, actor)
    terminal = len(approvers) == 2
    return replace(
        request,
        approver_ids=approvers,
        status=TEARDOWN_REQUEST_EXECUTION_DISABLED if terminal else TEARDOWN_REQUEST_PENDING,
        execution_result=TEARDOWN_EXECUTION_DISABLED_RESULT if terminal else "",
        updated_at=approved_at,
        completed_at=approved_at if terminal else "",
    )


def validate_module(module: DeploymentModule) -> None:
    if module.module_id not in MODULE_IDS:
        raise ValueError(f"Unknown module id: {module.module_id}")
    if not module.deployment_id.strip() or not module.version.strip():
        raise ValueError("Deployment id and module version are required.")


def validate_release(release: ReleaseManifest) -> None:
    if not release.version.strip() or not release.git_sha.strip():
        raise ValueError("Release version and git sha are required.")
    if not release.modules:
        raise ValueError("Release manifest must include module versions.")
    unknown = [module_id for module_id in release.modules if module_id not in MODULE_IDS]
    if unknown:
        raise ValueError(f"Unknown release modules: {unknown}")
    if release.rollback_kind not in ROLLBACK_KINDS:
        raise ValueError(f"Unknown rollback kind: {release.rollback_kind}")
    if release.images:
        if set(release.images) != set(release.modules):
            raise ValueError("Release images map must cover exactly the release's modules.")
        errors = [err for ref in release.images.values() if (err := validate_image_ref(ref))]
        if errors:
            raise ValueError("; ".join(errors))


def validate_promotion(promotion: ReleasePromotion) -> None:
    if not promotion.release_version.strip():
        raise ValueError("Release promotion version is required.")
    if promotion.state not in PROMOTION_STATES:
        raise ValueError(f"Unknown release promotion state: {promotion.state}")


def validate_run_status(status: str) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"Unknown run status: {status}")


def compute_update_plan(
    deployment_id: str,
    target_version: str,
    *,
    deployment,            # CustomerDeployment | None
    release,               # ReleaseManifest | None
    modules,               # list[DeploymentModule] (any status; filtered here)
    latest_backup,         # Callable[[], BackupRun | None] — invoked ONLY inside the backup gate (A3:
                           # today's stores query latest_backup only when the gate can fire; a plain value
                           # here would add one eager SELECT to every plan_update on the hot fleet path)
    ack_restore_required: bool = False,
    require_signed_release: bool = False,
    promotion=None,          # ReleasePromotion | None
    promotion_required: bool = False,
    promotion_warning_only: bool = False,
    gate_deployment_id: str = "",
    production_signature_valid: Optional[bool] = None,
    development_signature_valid: Optional[bool] = None,
    heartbeat_max_age_seconds: int = 600,
    now: Optional[datetime] = None,
    active_rollout: bool = False,
) -> UpdatePlan:
    """The single shared plan gate: both stores delegate here (never patch a
    store copy separately). Gate order is contract — see the Hetzner P0 spec."""
    if not deployment:
        return UpdatePlan(deployment_id, target_version, False, "deployment_not_found")
    if not release:
        return UpdatePlan(deployment_id, target_version, False, "release_not_found")
    kind = release.rollback_kind
    if release.status == "yanked":
        return UpdatePlan(deployment_id, target_version, False, "release_yanked", rollback_kind=kind)

    warnings: List[str] = []

    def promotion_denial() -> str:
        is_gate = bool(getattr(deployment, "is_release_gate", False))
        if is_gate:
            if not gate_deployment_id:
                return "development_gate_missing"
            if deployment.id != gate_deployment_id:
                return "development_gate_mismatch"
            if not promotion or promotion.gate_deployment_id not in {"", deployment.id}:
                return "development_gate_mismatch"
            # ``dev_failed`` is plan-eligible so the development dispatcher can
            # retry it. Desired-state generation still excludes that state; the
            # dispatcher must first persist a new rollout and transition the
            # promotion back to ``dev_deploying`` before a box can receive it.
            if promotion.state not in {"dev_pending", "dev_deploying", "dev_failed"}:
                return "release_not_dev_verified"
            if development_signature_valid is not True:
                return "release_signature_invalid"
        else:
            if not promotion:
                return "release_not_customer_approved"
            if promotion.state == "customer_paused":
                return "release_customer_paused"
            if promotion.state == "yanked":
                return "release_yanked"
            if promotion.state != "customer_approved":
                return "release_not_customer_approved"
            if not release.signature:
                return "release_unsigned"
            if production_signature_valid is not True:
                return "release_signature_invalid"
        if active_rollout:
            return "deployment_rollout_active"
        heartbeat_at = getattr(deployment, "last_heartbeat_at", "") or ""
        if not heartbeat_at:
            return "deployment_heartbeat_stale"
        try:
            received = datetime.fromisoformat(heartbeat_at)
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return "deployment_heartbeat_stale"
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        if (clock - received).total_seconds() > max(0, heartbeat_max_age_seconds):
            return "deployment_heartbeat_stale"
        if getattr(deployment, "last_heartbeat_healthy", None) is not True:
            return "deployment_unhealthy"
        return ""

    promotion_reason = promotion_denial()
    if promotion_reason:
        if promotion_required and not promotion_warning_only:
            return UpdatePlan(deployment_id, target_version, False, promotion_reason, rollback_kind=kind)
        warnings.append(promotion_reason)
    # Development candidates deliberately carry their verified signature on
    # the promotion row; the production signature is only attached after the
    # gate verifies the release. Treat that development signature as the
    # signed-release credential only for the designated gate and only while
    # the candidate is in a deployable development state.
    gate_development_signature_valid = bool(
        getattr(deployment, "is_release_gate", False)
        and gate_deployment_id == deployment.id
        and promotion
        and promotion.gate_deployment_id in {"", deployment.id}
        and promotion.state in {"dev_pending", "dev_deploying", "dev_failed"}
        and development_signature_valid is True
    )
    if require_signed_release and not release.signature and not gate_development_signature_valid:
        return UpdatePlan(
            deployment_id, target_version, False, "release_unsigned",
            rollback_kind=kind, warnings=warnings,
        )
    if effective_update_policy(deployment) == "pinned" and release.version != deployment.current_version:
        return UpdatePlan(deployment_id, target_version, False, "update_policy_pinned", rollback_kind=kind)
    current = {m.module_id: m.version for m in modules if m.status == "active"}
    if not current:
        return UpdatePlan(deployment_id, target_version, False, "no_modules_installed", rollback_kind=kind)
    missing = sorted(module_id for module_id in current if module_id not in release.modules)
    if missing:
        return UpdatePlan(
            deployment_id, target_version, False, f"release_missing_modules:{','.join(missing)}",
            current_modules=current, target_modules=release.modules, rollback_kind=kind)
    if kind == "restore_required" and effective_update_policy(deployment) == "auto" and not ack_restore_required:
        return UpdatePlan(
            deployment_id, target_version, False, "restore_required_ack_needed",
            current_modules=current, target_modules=release.modules, rollback_kind=kind)
    # B6: restore-required implies restorable. Comm's raw-SQL migrations run inside comm containers and
    # never move onebrain's migration_to, so a comm-only destructive release must ALSO have a fresh backup —
    # the rollback_kind condition covers it. Inert for legacy releases (kind == "").
    needs_fresh_backup = bool(
        (release.migration_to and release.migration_to != deployment.current_migration)
        or kind == "restore_required"
    )
    if needs_fresh_backup:
        backup = latest_backup()   # lazy: the only call site (A3)
        if not backup or backup.status != "success":
            return UpdatePlan(
                deployment_id, target_version, False, "backup_required_for_schema_update",
                current_modules=current, target_modules=release.modules, rollback_kind=kind)
    updates = {m: release.modules[m] for m, v in current.items() if v != release.modules[m]}
    return UpdatePlan(
        deployment_id, target_version, True,
        "update_available" if updates else "already_current",
        current_modules=current,
        target_modules={m: release.modules[m] for m in current},
        modules_to_update=updates, rollback_kind=kind, warnings=warnings)


def release_promotion_plan_context(release, promotion) -> Dict:
    """Settings and trust inputs for the shared planner, loaded identically by both stores."""
    try:
        from app.config import get_settings
        from app.trust.release import release_signature_fields, verify_release_signature
    except ImportError:
        return {
            "promotion_required": False,
            "promotion_warning_only": True,
            "heartbeat_max_age_seconds": 600,
        }
    settings = get_settings()
    production_valid = None
    development_valid = None
    production_key = getattr(settings, "release_verify_public_key", "")
    development_key = getattr(settings, "dev_release_verify_public_key", "")
    required = bool(getattr(settings, "release_promotion_required", False))
    if release and release.signature and production_key:
        production_valid = verify_release_signature(
            release_signature_fields(release), release.signature, production_key
        )
    if promotion and promotion.dev_signature and development_key and release:
        development_valid = verify_release_signature(
            release_signature_fields(release),
            promotion.dev_signature,
            development_key,
        )
    return {
        "promotion_required": required,
        "promotion_warning_only": not required,
        "production_signature_valid": production_valid,
        "development_signature_valid": development_valid,
        "heartbeat_max_age_seconds": max(600, int(getattr(settings, "fleet_report_seconds", 60)) * 2),
    }


def require_signed_releases() -> bool:
    """Settings-aware helper for the stores. FAILS CLOSED-adjacent (B7/C2): only
    ImportError (config module unavailable in minimal unit-test envs) maps to
    False; any real settings failure PROPAGATES loudly — a broken config must
    never silently disable a trust gate. NOTE (C2): operator endpoint tests
    monkeypatch operator_router.get_settings, which does NOT reach this helper's
    lazy import — store-level tests must monkeypatch app.config.get_settings."""
    try:
        from app.config import get_settings
    except ImportError:
        return False
    return bool(get_settings().release_require_signature)

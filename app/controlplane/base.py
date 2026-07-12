"""Control-plane records and store contract.

This domain tracks customer deployments and release state. It deliberately
stores deployment metadata only, never customer content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
DEPLOYMENT_TYPES = frozenset({"dedicated_railway", "shared_railway", "dedicated_server", "customer_owned"})
RUN_STATUSES = frozenset({"pending", "running", "success", "failed", "paused"})

ROLLBACK_KINDS = frozenset({"", "code_only", "restore_required"})
UPDATE_POLICIES = frozenset({"", "auto", "manual", "pinned"})
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
    deployment_type: str = "dedicated_railway"
    region: str = ""
    release_ring: str = "manual"
    status: str = "active"
    current_version: str = ""
    current_migration: str = ""
    created_at: str = ""
    update_policy: str = ""       # ''(legacy: derive from ring) | auto | manual | pinned


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
    # Execution lifecycle (a rollout is a dispatched GitHub-Actions update job).
    exec_status: str = "pending"          # see rollout_exec.ROLLOUT_EXEC_STATUSES
    external_provider: str = "github_actions"
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
class UpdatePlan:
    deployment_id: str
    target_version: str
    allowed: bool
    reason: str
    current_modules: Dict[str, str] = field(default_factory=dict)
    target_modules: Dict[str, str] = field(default_factory=dict)
    modules_to_update: Dict[str, str] = field(default_factory=dict)
    rollback_kind: str = ""       # copied from the release when found (operator UI/plan visibility)


class ControlPlaneStore(Protocol):
    def create_deployment(self, deployment: CustomerDeployment) -> CustomerDeployment: ...

    def get_deployment(self, deployment_id: str) -> Optional[CustomerDeployment]: ...

    def list_deployments(self) -> List[CustomerDeployment]: ...

    def upsert_module(self, module: DeploymentModule) -> DeploymentModule: ...

    def list_modules(self, deployment_id: str) -> List[DeploymentModule]: ...

    def create_release(self, release: ReleaseManifest) -> ReleaseManifest: ...

    def get_release(self, version: str) -> Optional[ReleaseManifest]: ...

    def list_releases(self) -> List[ReleaseManifest]: ...

    def record_backup(self, backup: BackupRun) -> BackupRun: ...

    def latest_backup(self, deployment_id: str) -> Optional[BackupRun]: ...

    def record_health(self, health: HealthCheckRun) -> HealthCheckRun: ...

    def latest_health(self, deployment_id: str) -> Optional[HealthCheckRun]: ...

    def plan_update(self, deployment_id: str, target_version: str, *, ack_restore_required: bool = False) -> UpdatePlan: ...

    def set_update_policy(self, deployment_id: str, update_policy: str) -> CustomerDeployment: ...

    def start_rollout(self, rollout: RolloutRun) -> RolloutRun: ...

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "", apply: bool = True) -> RolloutRun: ...

    def get_rollout(self, rollout_id: str) -> Optional[RolloutRun]: ...

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]: ...

    def list_active_rollout(self, deployment_id: str) -> Optional[RolloutRun]: ...

    def list_rollouts_for_fleet(self, fleet_rollout_id: str) -> List[RolloutRun]: ...

    def claim_rollout_dispatch(self, rollout_id: str) -> bool: ...

    def update_rollout_exec(self, rollout_id: str, **fields) -> RolloutRun: ...

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
    if require_signed_release and not release.signature:
        return UpdatePlan(deployment_id, target_version, False, "release_unsigned", rollback_kind=kind)
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
        modules_to_update=updates, rollback_kind=kind)


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

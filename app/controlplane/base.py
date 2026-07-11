"""Control-plane records and store contract.

This domain tracks customer deployments and release state. It deliberately
stores deployment metadata only, never customer content.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class UpdatePlan:
    deployment_id: str
    target_version: str
    allowed: bool
    reason: str
    current_modules: Dict[str, str] = field(default_factory=dict)
    target_modules: Dict[str, str] = field(default_factory=dict)
    modules_to_update: Dict[str, str] = field(default_factory=dict)


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

    def plan_update(self, deployment_id: str, target_version: str) -> UpdatePlan: ...

    def start_rollout(self, rollout: RolloutRun) -> RolloutRun: ...

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "", apply: bool = True) -> RolloutRun: ...

    def get_rollout(self, rollout_id: str) -> Optional[RolloutRun]: ...

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]: ...

    def list_active_rollout(self, deployment_id: str) -> Optional[RolloutRun]: ...

    def claim_rollout_dispatch(self, rollout_id: str) -> bool: ...

    def update_rollout_exec(self, rollout_id: str, **fields) -> RolloutRun: ...


def validate_deployment(deployment: CustomerDeployment) -> None:
    if not deployment.id.strip() or not deployment.customer_name.strip():
        raise ValueError("Deployment id and customer name are required.")
    if deployment.deployment_type not in DEPLOYMENT_TYPES:
        raise ValueError(f"Unknown deployment type: {deployment.deployment_type}")
    if deployment.release_ring not in ROLL_OUT_RINGS:
        raise ValueError(f"Unknown release ring: {deployment.release_ring}")


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


def validate_run_status(status: str) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"Unknown run status: {status}")

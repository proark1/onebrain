"""JSON-backed in-process control-plane store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from typing import Dict, List, Optional

from app.controlplane.base import (
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    RolloutRun,
    UpdatePlan,
    validate_deployment,
    validate_module,
    validate_release,
    validate_run_status,
)


class MemoryControlPlaneStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._deployments: Dict[str, CustomerDeployment] = {}
        self._modules: Dict[tuple[str, str], DeploymentModule] = {}
        self._releases: Dict[str, ReleaseManifest] = {}
        self._backups: Dict[str, BackupRun] = {}
        self._health: Dict[str, HealthCheckRun] = {}
        self._rollouts: Dict[str, RolloutRun] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._deployments = {d["id"]: CustomerDeployment(**d) for d in data.get("deployments", [])}
            self._modules = {
                (d["deployment_id"], d["module_id"]): DeploymentModule(**d)
                for d in data.get("modules", [])
            }
            self._releases = {d["version"]: ReleaseManifest(**d) for d in data.get("releases", [])}
            self._backups = {d["id"]: BackupRun(**d) for d in data.get("backups", [])}
            self._health = {d["id"]: HealthCheckRun(**d) for d in data.get("health", [])}
            self._rollouts = {d["id"]: RolloutRun(**d) for d in data.get("rollouts", [])}
        except Exception:
            self._deployments, self._modules, self._releases = {}, {}, {}
            self._backups, self._health, self._rollouts = {}, {}, {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({
                "deployments": [d.__dict__ for d in self._deployments.values()],
                "modules": [m.__dict__ for m in self._modules.values()],
                "releases": [r.__dict__ for r in self._releases.values()],
                "backups": [b.__dict__ for b in self._backups.values()],
                "health": [h.__dict__ for h in self._health.values()],
                "rollouts": [r.__dict__ for r in self._rollouts.values()],
            }, fh)

    def create_deployment(self, deployment: CustomerDeployment) -> CustomerDeployment:
        validate_deployment(deployment)
        with self._lock:
            if deployment.id in self._deployments:
                raise ValueError(f"deployment already exists: {deployment.id}")
            self._deployments[deployment.id] = deployment
            self._save()
            return deployment

    def get_deployment(self, deployment_id: str) -> Optional[CustomerDeployment]:
        return self._deployments.get(deployment_id)

    def list_deployments(self) -> List[CustomerDeployment]:
        return sorted(self._deployments.values(), key=lambda d: d.customer_name.lower())

    def upsert_module(self, module: DeploymentModule) -> DeploymentModule:
        validate_module(module)
        with self._lock:
            if module.deployment_id not in self._deployments:
                raise ValueError(f"unknown deployment: {module.deployment_id}")
            self._modules[(module.deployment_id, module.module_id)] = module
            self._save()
            return module

    def list_modules(self, deployment_id: str) -> List[DeploymentModule]:
        return sorted(
            (m for (dep_id, _), m in self._modules.items() if dep_id == deployment_id),
            key=lambda m: m.module_id,
        )

    def create_release(self, release: ReleaseManifest) -> ReleaseManifest:
        validate_release(release)
        with self._lock:
            if release.version in self._releases:
                raise ValueError(f"release already exists: {release.version}")
            self._releases[release.version] = release
            self._save()
            return release

    def get_release(self, version: str) -> Optional[ReleaseManifest]:
        return self._releases.get(version)

    def list_releases(self) -> List[ReleaseManifest]:
        return sorted(self._releases.values(), key=lambda r: r.version)

    def record_backup(self, backup: BackupRun) -> BackupRun:
        validate_run_status(backup.status)
        with self._lock:
            if backup.id in self._backups:
                raise ValueError(f"backup already exists: {backup.id}")
            if backup.deployment_id not in self._deployments:
                raise ValueError(f"unknown deployment: {backup.deployment_id}")
            self._backups[backup.id] = backup
            self._save()
            return backup

    def latest_backup(self, deployment_id: str) -> Optional[BackupRun]:
        backups = [b for b in self._backups.values() if b.deployment_id == deployment_id]
        return sorted(backups, key=lambda b: b.created_at or b.id)[-1] if backups else None

    def record_health(self, health: HealthCheckRun) -> HealthCheckRun:
        validate_run_status(health.status)
        with self._lock:
            if health.id in self._health:
                raise ValueError(f"health check already exists: {health.id}")
            if health.deployment_id not in self._deployments:
                raise ValueError(f"unknown deployment: {health.deployment_id}")
            self._health[health.id] = health
            self._save()
            return health

    def latest_health(self, deployment_id: str) -> Optional[HealthCheckRun]:
        checks = [h for h in self._health.values() if h.deployment_id == deployment_id]
        return sorted(checks, key=lambda h: h.created_at or h.id)[-1] if checks else None

    def plan_update(self, deployment_id: str, target_version: str) -> UpdatePlan:
        deployment = self.get_deployment(deployment_id)
        if not deployment:
            return UpdatePlan(deployment_id, target_version, False, "deployment_not_found")
        release = self.get_release(target_version)
        if not release:
            return UpdatePlan(deployment_id, target_version, False, "release_not_found")

        current = {m.module_id: m.version for m in self.list_modules(deployment_id) if m.status == "active"}
        if not current:
            return UpdatePlan(deployment_id, target_version, False, "no_modules_installed")
        missing = sorted(module_id for module_id in current if module_id not in release.modules)
        if missing:
            return UpdatePlan(
                deployment_id, target_version, False, f"release_missing_modules:{','.join(missing)}",
                current_modules=current, target_modules=release.modules,
            )
        if release.migration_to and release.migration_to != deployment.current_migration:
            latest = self.latest_backup(deployment_id)
            if not latest or latest.status != "success":
                return UpdatePlan(
                    deployment_id, target_version, False, "backup_required_for_schema_update",
                    current_modules=current, target_modules=release.modules,
                )
        updates = {
            module_id: target
            for module_id, current_version in current.items()
            for target in [release.modules[module_id]]
            if current_version != target
        }
        return UpdatePlan(
            deployment_id,
            target_version,
            True,
            "update_available" if updates else "already_current",
            current_modules=current,
            target_modules={module_id: release.modules[module_id] for module_id in current},
            modules_to_update=updates,
        )

    def start_rollout(self, rollout: RolloutRun) -> RolloutRun:
        validate_run_status(rollout.status)
        if rollout.status == "success":
            raise ValueError("rollout cannot start as success")
        plan = self.plan_update(rollout.deployment_id, rollout.target_version)
        if not plan.allowed:
            raise ValueError(f"rollout blocked: {plan.reason}")
        with self._lock:
            if rollout.id in self._rollouts:
                raise ValueError(f"rollout already exists: {rollout.id}")
            self._rollouts[rollout.id] = rollout
            self._save()
            return rollout

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "") -> RolloutRun:
        validate_run_status(status)
        with self._lock:
            rollout = self._rollouts.get(rollout_id)
            if not rollout:
                raise ValueError(f"unknown rollout: {rollout_id}")
            if rollout.status in {"success", "failed"} and status != rollout.status:
                raise ValueError("terminal rollout status cannot be changed")
            updated = replace(rollout, status=status, notes=notes.strip() or rollout.notes)

            if status == "success":
                plan = self.plan_update(rollout.deployment_id, rollout.target_version)
                if not plan.allowed:
                    raise ValueError(f"rollout completion blocked: {plan.reason}")
                release = self.get_release(rollout.target_version)
                deployment = self.get_deployment(rollout.deployment_id)
                if not release or not deployment:
                    raise ValueError("rollout target is no longer available")

                for module in self.list_modules(rollout.deployment_id):
                    if module.status != "active" or module.module_id not in release.modules:
                        continue
                    self._modules[(module.deployment_id, module.module_id)] = replace(
                        module,
                        version=release.modules[module.module_id],
                    )
                self._deployments[deployment.id] = replace(
                    deployment,
                    current_version=release.version,
                    current_migration=release.migration_to or deployment.current_migration,
                )

            self._rollouts[rollout_id] = updated
            self._save()
            return updated

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]:
        return [r for r in self._rollouts.values() if r.deployment_id == deployment_id]

"""JSON-backed in-process control-plane store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from typing import Dict, List, Optional

from app.controlplane.base import (
    UPDATE_POLICIES,
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    RolloutRun,
    UpdatePlan,
    compute_update_plan,
    require_signed_releases,
    validate_deployment,
    validate_module,
    validate_release,
    validate_run_status,
)
from app.controlplane.orchestration import FLEET_EXEC_FIELDS, FleetRolloutRun


class MemoryControlPlaneStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._deployments: Dict[str, CustomerDeployment] = {}
        self._modules: Dict[tuple[str, str], DeploymentModule] = {}
        self._releases: Dict[str, ReleaseManifest] = {}
        self._backups: Dict[str, BackupRun] = {}
        self._health: Dict[str, HealthCheckRun] = {}
        self._rollouts: Dict[str, RolloutRun] = {}
        self._fleet_rollouts: Dict[str, FleetRolloutRun] = {}
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
            self._fleet_rollouts = {
                d["id"]: FleetRolloutRun(**{**d, "ring_order": tuple(d.get("ring_order", []))})
                for d in data.get("fleet_rollouts", [])
            }
        except Exception:
            self._deployments, self._modules, self._releases = {}, {}, {}
            self._backups, self._health, self._rollouts = {}, {}, {}
            self._fleet_rollouts = {}

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
                "fleet_rollouts": [{**f.__dict__, "ring_order": list(f.ring_order)}
                                   for f in self._fleet_rollouts.values()],
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

    def set_update_policy(self, deployment_id: str, update_policy: str) -> CustomerDeployment:
        if update_policy not in UPDATE_POLICIES or not update_policy:
            raise ValueError(f"Unknown update policy: {update_policy}")
        with self._lock:
            deployment = self._deployments.get(deployment_id)
            if not deployment:
                raise ValueError(f"unknown deployment: {deployment_id}")
            updated = replace(deployment, update_policy=update_policy)
            self._deployments[deployment_id] = updated
            self._save()
            return updated

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

    def plan_update(self, deployment_id: str, target_version: str, *, ack_restore_required: bool = False) -> UpdatePlan:
        deployment = self.get_deployment(deployment_id)
        return compute_update_plan(
            deployment_id, target_version,
            deployment=deployment,
            release=self.get_release(target_version),
            modules=self.list_modules(deployment_id) if deployment else [],
            latest_backup=lambda: self.latest_backup(deployment_id),  # lazy (A3); compute_update_plan
                                                                      # returns before calling it when
                                                                      # deployment is None
            ack_restore_required=ack_restore_required,
            require_signed_release=require_signed_releases(),
        )

    def start_rollout(self, rollout: RolloutRun) -> RolloutRun:
        validate_run_status(rollout.status)
        if rollout.status == "success":
            raise ValueError("rollout cannot start as success")
        plan = self.plan_update(rollout.deployment_id, rollout.target_version,
                                ack_restore_required=rollout.ack_restore_required)
        if not plan.allowed:
            raise ValueError(f"rollout blocked: {plan.reason}")
        with self._lock:
            if rollout.id in self._rollouts:
                raise ValueError(f"rollout already exists: {rollout.id}")
            self._rollouts[rollout.id] = rollout
            self._save()
            return rollout

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "", apply: bool = True) -> RolloutRun:
        validate_run_status(status)
        with self._lock:
            rollout = self._rollouts.get(rollout_id)
            if not rollout:
                raise ValueError(f"unknown rollout: {rollout_id}")
            if rollout.status in {"success", "failed"} and status != rollout.status:
                raise ValueError("terminal rollout status cannot be changed")
            updated = replace(rollout, status=status, notes=notes.strip() or rollout.notes)

            if status == "success" and apply:
                plan = self.plan_update(rollout.deployment_id, rollout.target_version,
                                        ack_restore_required=rollout.ack_restore_required)
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

    def get_rollout(self, rollout_id: str) -> Optional[RolloutRun]:
        return self._rollouts.get(rollout_id)

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]:
        return [r for r in self._rollouts.values() if r.deployment_id == deployment_id]

    def list_active_rollout(self, deployment_id: str) -> Optional[RolloutRun]:
        """A non-terminal rollout for this deployment (concurrency guard) or None."""
        for rollout in self._rollouts.values():
            if rollout.deployment_id == deployment_id and rollout.status not in {"success", "failed"}:
                return rollout
        return None

    def list_rollouts_for_fleet(self, fleet_rollout_id: str) -> List[RolloutRun]:
        return [r for r in self._rollouts.values() if r.fleet_rollout_id == fleet_rollout_id]

    def claim_rollout_dispatch(self, rollout_id: str) -> bool:
        """Atomically claim a pending rollout for dispatch (compare-and-set
        exec_status pending->dispatched). Returns False if it is not pending — the
        guard that prevents two concurrent dispatches firing two real update jobs."""
        with self._lock:
            rollout = self._rollouts.get(rollout_id)
            if not rollout or rollout.exec_status != "pending":
                return False
            self._rollouts[rollout_id] = replace(rollout, exec_status="dispatched")
            self._save()
            return True

    # --- fleet rollouts (Phase 2 orchestration) ---
    def create_fleet_rollout(self, fleet_run: FleetRolloutRun) -> FleetRolloutRun:
        with self._lock:
            if fleet_run.id in self._fleet_rollouts:
                raise ValueError(f"fleet rollout already exists: {fleet_run.id}")
            self._fleet_rollouts[fleet_run.id] = fleet_run
            self._save()
            return fleet_run

    def get_fleet_rollout(self, fleet_rollout_id: str) -> Optional[FleetRolloutRun]:
        return self._fleet_rollouts.get(fleet_rollout_id)

    def list_fleet_rollouts(self) -> List[FleetRolloutRun]:
        return sorted(self._fleet_rollouts.values(), key=lambda f: (f.created_at, f.id))

    def update_fleet_rollout(self, fleet_rollout_id: str, **fields) -> FleetRolloutRun:
        bad = set(fields) - FLEET_EXEC_FIELDS
        if bad:
            raise ValueError(f"cannot update fleet rollout fields: {sorted(bad)}")
        with self._lock:
            fleet_run = self._fleet_rollouts.get(fleet_rollout_id)
            if not fleet_run:
                raise ValueError(f"unknown fleet rollout: {fleet_rollout_id}")
            updated = replace(fleet_run, **fields)
            self._fleet_rollouts[fleet_rollout_id] = updated
            self._save()
            return updated

    def advance_fleet_ring(self, fleet_rollout_id: str, from_ring: str, to_ring: str) -> bool:
        """Atomically move a running fleet rollout from one ring to the next
        (compare-and-set on current_ring). Only the winner opens the next ring, so
        concurrent child callbacks can't double-dispatch it."""
        with self._lock:
            fleet_run = self._fleet_rollouts.get(fleet_rollout_id)
            if not fleet_run or fleet_run.status != "running" or fleet_run.current_ring != from_ring:
                return False
            self._fleet_rollouts[fleet_rollout_id] = replace(fleet_run, current_ring=to_ring)
            self._save()
            return True

    _EXEC_FIELDS = {
        "exec_status", "external_run_id", "external_run_url", "failure_reason",
        "dispatched_at", "completed_at", "request_payload", "fleet_rollout_id",
    }

    def update_rollout_exec(self, rollout_id: str, **fields) -> RolloutRun:
        """Persist execution-lifecycle fields only. Bookkeeping status transitions
        (and the version-apply) go through update_rollout_status; this keeps them
        cleanly separated. Guarding (monotonic rank / terminal) is done by the pure
        apply_rollout_callback before it calls here."""
        bad = set(fields) - self._EXEC_FIELDS
        if bad:
            raise ValueError(f"cannot update rollout exec fields: {sorted(bad)}")
        with self._lock:
            rollout = self._rollouts.get(rollout_id)
            if not rollout:
                raise ValueError(f"unknown rollout: {rollout_id}")
            updated = replace(rollout, **fields)
            self._rollouts[rollout_id] = updated
            self._save()
            return updated

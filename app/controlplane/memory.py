"""JSON-backed in-process control-plane store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.controlplane.base import (
    UPDATE_POLICIES,
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    ReleasePromotion,
    ReleasePromotionEvent,
    RolloutRun,
    ServedFloorBump,
    UpdatePlan,
    compute_update_plan,
    release_promotion_plan_context,
    require_signed_releases,
    validate_deployment,
    validate_module,
    validate_promotion,
    validate_release,
    validate_run_status,
)
from app.controlplane.orchestration import FLEET_EXEC_FIELDS, FleetRolloutRun


class MemoryControlPlaneStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._deployments: Dict[str, CustomerDeployment] = {}
        self._modules: Dict[tuple[str, str], DeploymentModule] = {}
        self._releases: Dict[str, ReleaseManifest] = {}
        self._release_promotions: Dict[str, ReleasePromotion] = {}
        self._release_promotion_events: List[ReleasePromotionEvent] = []
        self._backups: Dict[str, BackupRun] = {}
        self._health: Dict[str, HealthCheckRun] = {}
        self._rollouts: Dict[str, RolloutRun] = {}
        self._fleet_rollouts: Dict[str, FleetRolloutRun] = {}
        self._served_floor_bumps: Dict[str, ServedFloorBump] = {}
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
            self._release_promotions = {
                d["release_version"]: ReleasePromotion(**d)
                for d in data.get("release_promotions", [])
            }
            self._release_promotion_events = [
                ReleasePromotionEvent(**d) for d in data.get("release_promotion_events", [])
            ]
            self._backups = {d["id"]: BackupRun(**d) for d in data.get("backups", [])}
            self._health = {d["id"]: HealthCheckRun(**d) for d in data.get("health", [])}
            self._rollouts = {d["id"]: RolloutRun(**d) for d in data.get("rollouts", [])}
            self._fleet_rollouts = {
                d["id"]: FleetRolloutRun(**{
                    **d,
                    "ring_order": tuple(d.get("ring_order", [])),
                    "only_deployment_ids": tuple(d.get("only_deployment_ids", [])),
                    "updated_at": d.get("updated_at", d.get("created_at", "")),
                })
                for d in data.get("fleet_rollouts", [])
            }
            # Additive (P5-01), back-compatible with a persist file written before Phase 5.
            self._served_floor_bumps = {
                d["scope"]: ServedFloorBump(**d) for d in data.get("served_floor_bumps", [])
            }
        except Exception:
            self._deployments, self._modules, self._releases = {}, {}, {}
            self._release_promotions, self._release_promotion_events = {}, []
            self._backups, self._health, self._rollouts = {}, {}, {}
            self._fleet_rollouts = {}
            self._served_floor_bumps = {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({
                "deployments": [d.__dict__ for d in self._deployments.values()],
                "modules": [m.__dict__ for m in self._modules.values()],
                "releases": [r.__dict__ for r in self._releases.values()],
                "release_promotions": [p.__dict__ for p in self._release_promotions.values()],
                "release_promotion_events": [e.__dict__ for e in self._release_promotion_events],
                "backups": [b.__dict__ for b in self._backups.values()],
                "health": [h.__dict__ for h in self._health.values()],
                "rollouts": [r.__dict__ for r in self._rollouts.values()],
                "fleet_rollouts": [{
                    **f.__dict__,
                    "ring_order": list(f.ring_order),
                    "only_deployment_ids": list(f.only_deployment_ids),
                }
                                   for f in self._fleet_rollouts.values()],
                "served_floor_bumps": [b.__dict__ for b in self._served_floor_bumps.values()],
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
        return sorted(
            self._releases.values(),
            key=lambda release: (release.created_at, release.version),
            reverse=True,
        )

    def create_release_candidate(
        self,
        release: ReleaseManifest,
        promotion: ReleasePromotion,
        event: ReleasePromotionEvent,
    ) -> ReleasePromotion:
        validate_release(release)
        validate_promotion(promotion)
        if promotion.release_version != release.version or event.release_version != release.version:
            raise ValueError("release candidate records must use the same version")
        with self._lock:
            if release.version in self._releases or release.version in self._release_promotions:
                raise ValueError(f"release candidate already exists: {release.version}")
            self._releases[release.version] = release
            self._release_promotions[release.version] = promotion
            self._release_promotion_events.append(event)
            self._save()
            return promotion

    def get_release_promotion(self, version: str) -> Optional[ReleasePromotion]:
        return self._release_promotions.get(version)

    def list_release_promotions(self) -> List[ReleasePromotion]:
        return sorted(
            self._release_promotions.values(),
            key=lambda promotion: (promotion.created_at, promotion.release_version),
            reverse=True,
        )

    _PROMOTION_FIELDS = frozenset({
        "gate_deployment_id", "dev_signature", "dev_signing_key_id", "dev_rollout_id",
        "dev_attempt_id", "dev_started_at", "dev_completed_at", "dev_verified_at",
        "customer_approved_at", "customer_approved_by", "customer_paused_at",
        "customer_paused_reason", "yanked_at", "failure_reason", "updated_at",
    })

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
    ) -> ReleasePromotion:
        fields = dict(fields or {})
        bad = set(fields) - self._PROMOTION_FIELDS
        if bad:
            raise ValueError(f"cannot update release promotion fields: {sorted(bad)}")
        validate_promotion(ReleasePromotion(release_version=version, state=to_state))
        with self._lock:
            promotion = self._release_promotions.get(version)
            if not promotion:
                raise ValueError(f"unknown release promotion: {version}")
            if promotion.state not in from_states:
                raise ValueError(
                    f"release promotion state changed: expected {sorted(from_states)}, got {promotion.state}"
                )
            fields.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
            updated = replace(promotion, state=to_state, **fields)
            self._release_promotions[version] = updated
            release = self._releases.get(version)
            if release and to_state == "yanked":
                self._releases[version] = replace(release, status="yanked")
            elif release and to_state == "customer_approved":
                self._releases[version] = replace(release, status="active")
            self._release_promotion_events.append(ReleasePromotionEvent(
                id=f"event-{len(self._release_promotion_events) + 1}",
                release_version=version,
                actor=actor,
                action=action,
                from_state=promotion.state,
                to_state=to_state,
                note=note,
                metadata=fields,
                created_at=fields["updated_at"],
            ))
            self._save()
            return updated

    def list_release_promotion_events(self, version: str) -> List[ReleasePromotionEvent]:
        return [event for event in self._release_promotion_events if event.release_version == version]

    def set_release_production_signature(
        self,
        version: str,
        *,
        signature: str,
        signing_key_id: str,
        actor: str,
    ) -> ReleaseManifest:
        with self._lock:
            release = self._releases.get(version)
            promotion = self._release_promotions.get(version)
            if not release or not promotion:
                raise ValueError(f"unknown release candidate: {version}")
            if promotion.state != "dev_verified":
                raise ValueError("release_not_dev_verified")
            if release.signature:
                if release.signature == signature and release.signing_key_id == signing_key_id:
                    return release
                raise ValueError("production_signature_already_attached")
            updated = replace(release, signature=signature, signing_key_id=signing_key_id)
            self._releases[version] = updated
            now = datetime.now(timezone.utc).isoformat()
            self._release_promotion_events.append(ReleasePromotionEvent(
                id=f"event-{len(self._release_promotion_events) + 1}",
                release_version=version,
                actor=actor,
                action="production_signature_attached",
                from_state=promotion.state,
                to_state=promotion.state,
                metadata={"signing_key_id": signing_key_id},
                created_at=now,
            ))
            self._save()
            return updated

    def approve_release_for_customers(
        self,
        version: str,
        *,
        signature: str,
        signing_key_id: str,
        actor: str,
        note: str = "",
    ) -> ReleasePromotion:
        if not signature or not signing_key_id:
            raise ValueError("production signature and signing key id are required")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            release = self._releases.get(version)
            promotion = self._release_promotions.get(version)
            if not release or not promotion:
                raise ValueError(f"unknown release candidate: {version}")
            if promotion.state != "dev_verified":
                raise ValueError(f"release must be dev_verified, got {promotion.state}")
            if release.signature != signature or release.signing_key_id != signing_key_id:
                raise ValueError("production signature must be attached before approval")
            self._releases[version] = replace(
                release,
                status="active",
                signature=signature,
                signing_key_id=signing_key_id,
            )
            updated = replace(
                promotion,
                state="customer_approved",
                customer_approved_at=now,
                customer_approved_by=actor,
                failure_reason="",
                updated_at=now,
            )
            self._release_promotions[version] = updated
            self._release_promotion_events.append(ReleasePromotionEvent(
                id=f"event-{len(self._release_promotion_events) + 1}",
                release_version=version,
                actor=actor,
                action="customer_approved",
                from_state=promotion.state,
                to_state="customer_approved",
                note=note,
                metadata={"signing_key_id": signing_key_id},
                created_at=now,
            ))
            self._save()
            return updated

    def get_release_gate(self) -> Optional[CustomerDeployment]:
        return next(
            (deployment for deployment in self._deployments.values()
             if deployment.is_release_gate and deployment.status == "active"),
            None,
        )

    def designate_release_gate(self, deployment_id: str) -> CustomerDeployment:
        with self._lock:
            deployment = self._deployments.get(deployment_id)
            if not deployment:
                raise ValueError(f"unknown deployment: {deployment_id}")
            if deployment.environment != "development" or deployment.deployment_type != "dedicated_server":
                raise ValueError("release gate must be a dedicated development server")
            if deployment.status != "active":
                raise ValueError("release gate must be active")
            for existing_id, existing in tuple(self._deployments.items()):
                if existing.is_release_gate and existing_id != deployment_id:
                    self._deployments[existing_id] = replace(existing, is_release_gate=False)
            updated = replace(deployment, is_release_gate=True)
            self._deployments[deployment_id] = updated
            self._save()
            return updated

    def update_deployment_telemetry(
        self,
        deployment_id: str,
        *,
        heartbeat_at: str,
        healthy: bool,
        reported_version: str = "",
        reported_migration: str = "",
    ) -> CustomerDeployment:
        with self._lock:
            deployment = self._deployments.get(deployment_id)
            if not deployment:
                raise ValueError(f"unknown deployment: {deployment_id}")
            updated = replace(
                deployment,
                last_heartbeat_at=heartbeat_at,
                last_heartbeat_healthy=healthy,
                last_reported_version=reported_version,
                last_reported_migration=reported_migration,
            )
            self._deployments[deployment_id] = updated
            self._save()
            return updated

    def mark_deployment_provisioned(
        self,
        deployment_id: str,
        *,
        installed_at: str,
        version: str,
        migration: str = "",
    ) -> CustomerDeployment:
        with self._lock:
            deployment = self._deployments.get(deployment_id)
            if not deployment:
                raise ValueError(f"unknown deployment: {deployment_id}")
            updated = replace(
                deployment,
                current_version=version or deployment.current_version,
                current_migration=migration or deployment.current_migration,
                current_version_deployed_at=installed_at,
            )
            self._deployments[deployment_id] = updated
            self._save()
            return updated

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
        return max(backups, key=lambda backup: (backup.created_at, backup.id)) if backups else None

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
        return max(checks, key=lambda health: (health.created_at, health.id)) if checks else None

    def plan_update(
        self,
        deployment_id: str,
        target_version: str,
        *,
        ack_restore_required: bool = False,
        ignore_rollout_id: str = "",
    ) -> UpdatePlan:
        return self._plan_update(
            deployment_id,
            target_version,
            ack_restore_required=ack_restore_required,
            ignore_rollout_id=ignore_rollout_id,
        )

    def _plan_update(
        self,
        deployment_id: str,
        target_version: str,
        *,
        ack_restore_required: bool,
        ignore_rollout_id: str,
    ) -> UpdatePlan:
        deployment = self.get_deployment(deployment_id)
        release = self.get_release(target_version)
        promotion = self.get_release_promotion(target_version)
        gate = self.get_release_gate()
        return compute_update_plan(
            deployment_id, target_version,
            deployment=deployment,
            release=release,
            modules=self.list_modules(deployment_id) if deployment else [],
            latest_backup=lambda: self.latest_backup(deployment_id),  # lazy (A3); compute_update_plan
                                                                      # returns before calling it when
                                                                      # deployment is None
            ack_restore_required=ack_restore_required,
            require_signed_release=require_signed_releases(),
            promotion=promotion,
            gate_deployment_id=gate.id if gate else "",
            active_rollout=bool(
                (active := self.list_active_rollout(deployment_id))
                and active.id != ignore_rollout_id
            ),
            **release_promotion_plan_context(release, promotion),
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
                plan = self._plan_update(
                    rollout.deployment_id,
                    rollout.target_version,
                    ack_restore_required=rollout.ack_restore_required,
                    ignore_rollout_id=rollout.id,
                )
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
                    current_version_deployed_at=datetime.now(timezone.utc).isoformat(),
                )

            self._rollouts[rollout_id] = updated
            self._save()
            return updated

    def get_rollout(self, rollout_id: str) -> Optional[RolloutRun]:
        return self._rollouts.get(rollout_id)

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]:
        return sorted(
            (rollout for rollout in self._rollouts.values() if rollout.deployment_id == deployment_id),
            key=lambda rollout: (rollout.created_at, rollout.id),
            reverse=True,
        )

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
            fleet_run = replace(fleet_run, updated_at=fleet_run.updated_at or fleet_run.created_at)
            self._fleet_rollouts[fleet_run.id] = fleet_run
            self._save()
            return fleet_run

    def get_fleet_rollout(self, fleet_rollout_id: str) -> Optional[FleetRolloutRun]:
        return self._fleet_rollouts.get(fleet_rollout_id)

    def list_fleet_rollouts(self) -> List[FleetRolloutRun]:
        return sorted(self._fleet_rollouts.values(), key=lambda f: (f.updated_at or f.created_at, f.id), reverse=True)

    def update_fleet_rollout(self, fleet_rollout_id: str, **fields) -> FleetRolloutRun:
        bad = set(fields) - FLEET_EXEC_FIELDS
        if bad:
            raise ValueError(f"cannot update fleet rollout fields: {sorted(bad)}")
        with self._lock:
            fleet_run = self._fleet_rollouts.get(fleet_rollout_id)
            if not fleet_run:
                raise ValueError(f"unknown fleet rollout: {fleet_rollout_id}")
            updated = replace(fleet_run, **fields, updated_at=datetime.now(timezone.utc).isoformat())
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
            self._fleet_rollouts[fleet_rollout_id] = replace(
                fleet_run,
                current_ring=to_ring,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
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

    # --- served floor bumps (P5-01) ---
    def set_served_floor_bump(self, bump: ServedFloorBump) -> ServedFloorBump:
        with self._lock:
            self._served_floor_bumps[bump.scope] = bump
            self._save()
            return bump

    def clear_served_floor_bump(self, scope: str) -> bool:
        with self._lock:
            existed = self._served_floor_bumps.pop(scope, None) is not None
            if existed:
                self._save()
            return existed

    def get_served_floor_bump(self, scope: str) -> Optional[ServedFloorBump]:
        return self._served_floor_bumps.get(scope)

    def list_served_floor_bumps(self) -> List[ServedFloorBump]:
        return sorted(self._served_floor_bumps.values(), key=lambda b: b.scope)

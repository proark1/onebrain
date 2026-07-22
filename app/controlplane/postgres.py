"""Postgres-backed operator control-plane store."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.controlplane.base import (
    UPDATE_POLICIES,
    BackupRun,
    CustomerTeardownRequest,
    CustomerDeployment,
    DeploymentModule,
    HealthCheckRun,
    ReleaseManifest,
    ReleasePromotion,
    ReleasePromotionEvent,
    RolloutRun,
    ServedFloorBump,
    UpdatePlan,
    apply_teardown_approval,
    apply_teardown_execution,
    compute_update_plan,
    release_promotion_plan_context,
    require_signed_releases,
    validate_deployment,
    validate_module,
    validate_promotion,
    validate_release,
    validate_run_status,
    validate_teardown_request,
)
from app.controlplane.orchestration import FLEET_EXEC_FIELDS, FleetRolloutRun
from app.db.schema import validate_postgres_schema


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _json_dict(value) -> Dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        return {str(k): str(v) for k, v in json.loads(value or "{}").items()}
    return {str(k): str(v) for k, v in dict(value).items()}


def _json_list(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return tuple(str(item) for item in (value or ()))


class PostgresControlPlaneStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(
                conn,
                (
                    "control_deployments",
                    "control_deployment_modules",
                    "control_release_manifests",
                    "control_backups",
                    "control_health_checks",
                    "control_rollouts",
                    "control_fleet_rollouts",
                    "control_served_floor_bumps",
                    "control_release_promotions",
                    "control_release_promotion_events",
                    "control_customer_teardown_requests",
                ),
            )

    _DEPLOYMENT_COLS = (
        "id, customer_name, environment, deployment_type, region, release_ring, "
        "status, current_version, current_migration, created_at, account_id, update_policy, "
        "is_release_gate, current_version_deployed_at, last_heartbeat_at, "
        "last_heartbeat_healthy, last_reported_version, last_reported_migration, selected_module_ids, "
        "removed_at"
    )

    def create_deployment(self, deployment: CustomerDeployment) -> CustomerDeployment:
        validate_deployment(deployment)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO control_deployments
                (id, customer_name, environment, deployment_type, region, release_ring,
                 status, current_version, current_migration, account_id, update_policy,
                 is_release_gate, current_version_deployed_at, selected_module_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING {self._DEPLOYMENT_COLS}
                """,
                (
                    deployment.id,
                    deployment.customer_name,
                    deployment.environment,
                    deployment.deployment_type,
                    deployment.region,
                    deployment.release_ring,
                    deployment.status,
                    deployment.current_version,
                    deployment.current_migration,
                    deployment.account_id,
                    deployment.update_policy,
                    deployment.is_release_gate,
                    deployment.current_version_deployed_at or None,
                    json.dumps(list(deployment.selected_module_ids)),
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._deployment(row)

    def get_deployment(self, deployment_id: str) -> Optional[CustomerDeployment]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._DEPLOYMENT_COLS} FROM control_deployments "
                "WHERE id = %s AND removed_at IS NULL",
                (deployment_id,),
            )
            row = cur.fetchone()
        return self._deployment(row) if row else None

    def list_deployments(self) -> List[CustomerDeployment]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._DEPLOYMENT_COLS} FROM control_deployments "
                "WHERE removed_at IS NULL ORDER BY lower(customer_name), id"
            )
            rows = cur.fetchall()
        return [self._deployment(row) for row in rows]

    def remove_deployment(self, deployment_id: str, *, removed_at: str) -> CustomerDeployment:
        # Tombstone: hidden from list_deployments / fleet_overview, kept for audit.
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_deployments SET removed_at = COALESCE(%s::timestamptz, now()) "
                f"WHERE id = %s RETURNING {self._DEPLOYMENT_COLS}",
                (removed_at or None, deployment_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown deployment: {deployment_id}")
            conn.commit()
        return self._deployment(row)

    def set_update_policy(self, deployment_id: str, update_policy: str) -> CustomerDeployment:
        if update_policy not in UPDATE_POLICIES or not update_policy:
            raise ValueError(f"Unknown update policy: {update_policy}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_deployments SET update_policy = %s WHERE id = %s "
                f"RETURNING {self._DEPLOYMENT_COLS}",
                (update_policy, deployment_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown deployment: {deployment_id}")
            conn.commit()
        return self._deployment(row)

    def upsert_module(self, module: DeploymentModule) -> DeploymentModule:
        validate_module(module)
        if not self.get_deployment(module.deployment_id):
            raise ValueError(f"unknown deployment: {module.deployment_id}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_deployment_modules
                (deployment_id, module_id, version, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (deployment_id, module_id) DO UPDATE SET
                    version = EXCLUDED.version,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING deployment_id, module_id, version, status
                """,
                (module.deployment_id, module.module_id, module.version, module.status),
            )
            row = cur.fetchone()
            conn.commit()
        return self._module(row)

    def list_modules(self, deployment_id: str) -> List[DeploymentModule]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT deployment_id, module_id, version, status
                FROM control_deployment_modules
                WHERE deployment_id = %s
                ORDER BY module_id
                """,
                (deployment_id,),
            )
            rows = cur.fetchall()
        return [self._module(row) for row in rows]

    def create_release(self, release: ReleaseManifest) -> ReleaseManifest:
        validate_release(release)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_release_manifests
                (version, git_sha, modules, migration_from, migration_to,
                 security_notes, rollback_plan, status, images, rollback_kind, signature, signing_key_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING version, git_sha, modules, migration_from, migration_to,
                    security_notes, rollback_plan, status, created_at, images, rollback_kind, signature, signing_key_id
                """,
                (
                    release.version,
                    release.git_sha,
                    json.dumps(release.modules),
                    release.migration_from,
                    release.migration_to,
                    release.security_notes,
                    release.rollback_plan,
                    release.status,
                    json.dumps(release.images),
                    release.rollback_kind,
                    release.signature,
                    release.signing_key_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._release(row)

    def get_release(self, version: str) -> Optional[ReleaseManifest]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, git_sha, modules, migration_from, migration_to,
                    security_notes, rollback_plan, status, created_at, images, rollback_kind, signature, signing_key_id
                FROM control_release_manifests
                WHERE version = %s
                """,
                (version,),
            )
            row = cur.fetchone()
        return self._release(row) if row else None

    def list_releases(self) -> List[ReleaseManifest]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, git_sha, modules, migration_from, migration_to,
                    security_notes, rollback_plan, status, created_at, images, rollback_kind, signature, signing_key_id
                FROM control_release_manifests
                ORDER BY created_at DESC, version DESC
                """
            )
            rows = cur.fetchall()
        return [self._release(row) for row in rows]

    _PROMOTION_COLS = (
        "release_version, state, gate_deployment_id, dev_signature, dev_signing_key_id, "
        "dev_rollout_id, dev_attempt_id, dev_started_at, dev_completed_at, dev_verified_at, "
        "customer_approved_at, customer_approved_by, customer_paused_at, "
        "customer_paused_reason, yanked_at, failure_reason, created_at, updated_at"
    )
    _PROMOTION_FIELDS = frozenset({
        "gate_deployment_id", "dev_signature", "dev_signing_key_id", "dev_rollout_id",
        "dev_attempt_id", "dev_started_at", "dev_completed_at", "dev_verified_at",
        "customer_approved_at", "customer_approved_by", "customer_paused_at",
        "customer_paused_reason", "yanked_at", "failure_reason", "updated_at",
    })
    _PROMOTION_TIMESTAMPS = frozenset({
        "dev_started_at", "dev_completed_at", "dev_verified_at", "customer_approved_at",
        "customer_paused_at", "yanked_at", "updated_at",
    })

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
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_release_manifests
                (version, git_sha, modules, migration_from, migration_to, security_notes,
                 rollback_plan, status, images, rollback_kind, signature, signing_key_id)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (
                    release.version, release.git_sha, json.dumps(release.modules),
                    release.migration_from, release.migration_to, release.security_notes,
                    release.rollback_plan, release.status, json.dumps(release.images),
                    release.rollback_kind, release.signature, release.signing_key_id,
                ),
            )
            cur.execute(
                f"""
                INSERT INTO control_release_promotions
                (release_version, state, gate_deployment_id, dev_signature, dev_signing_key_id,
                 dev_rollout_id, dev_attempt_id, failure_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {self._PROMOTION_COLS}
                """,
                (
                    promotion.release_version, promotion.state,
                    promotion.gate_deployment_id or None, promotion.dev_signature,
                    promotion.dev_signing_key_id, promotion.dev_rollout_id or None,
                    promotion.dev_attempt_id, promotion.failure_reason,
                ),
            )
            row = cur.fetchone()
            self._insert_promotion_event(cur, event)
            conn.commit()
        return self._promotion(row)

    def get_release_promotion(self, version: str) -> Optional[ReleasePromotion]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._PROMOTION_COLS} FROM control_release_promotions "
                "WHERE release_version = %s",
                (version,),
            )
            row = cur.fetchone()
        return self._promotion(row) if row else None

    def list_release_promotions(self) -> List[ReleasePromotion]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._PROMOTION_COLS} FROM control_release_promotions "
                "ORDER BY created_at DESC, release_version DESC"
            )
            rows = cur.fetchall()
        return [self._promotion(row) for row in rows]

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
        updated_at = fields.pop("updated_at", "")
        sets = ["state = %s"]
        params: List = [to_state]
        if updated_at:
            sets.append("updated_at = %s::timestamptz")
            params.append(updated_at)
        else:
            sets.append("updated_at = now()")
        for key, value in fields.items():
            if key in self._PROMOTION_TIMESTAMPS:
                sets.append(f"{key} = %s::timestamptz")
                params.append(value or None)
            else:
                sets.append(f"{key} = %s")
                params.append(value or None if key in {"gate_deployment_id", "dev_rollout_id"} else value)
        params.extend([version, list(from_states)])
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE control_release_promotions SET {', '.join(sets)} "
                "WHERE release_version = %s AND state = ANY(%s) "
                f"RETURNING {self._PROMOTION_COLS}",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT state FROM control_release_promotions WHERE release_version = %s",
                    (version,),
                )
                got = cur.fetchone()
                if not got:
                    raise ValueError(f"unknown release promotion: {version}")
                raise ValueError(
                    f"release promotion state changed: expected {sorted(from_states)}, got {got[0]}"
                )
            previous_state = next(iter(from_states)) if len(from_states) == 1 else ""
            if to_state == "yanked":
                cur.execute(
                    "UPDATE control_release_manifests SET status = 'yanked' WHERE version = %s",
                    (version,),
                )
            elif to_state == "customer_approved":
                cur.execute(
                    "UPDATE control_release_manifests SET status = 'active' WHERE version = %s",
                    (version,),
                )
            self._insert_promotion_event(cur, ReleasePromotionEvent(
                id="",
                release_version=version,
                actor=actor,
                action=action,
                from_state=previous_state,
                to_state=to_state,
                note=note,
                metadata=fields,
            ))
            conn.commit()
        return self._promotion(row)

    def list_release_promotion_events(self, version: str) -> List[ReleasePromotionEvent]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, release_version, actor, action, from_state, to_state, note, metadata, created_at "
                "FROM control_release_promotion_events WHERE release_version = %s "
                "ORDER BY created_at ASC, id ASC",
                (version,),
            )
            rows = cur.fetchall()
        return [self._promotion_event(row) for row in rows]

    def set_release_production_signature(
        self,
        version: str,
        *,
        signature: str,
        signing_key_id: str,
        actor: str,
    ) -> ReleaseManifest:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT promotion.state, manifest.signature, manifest.signing_key_id "
                "FROM control_release_promotions AS promotion "
                "JOIN control_release_manifests AS manifest "
                "ON manifest.version = promotion.release_version "
                "WHERE promotion.release_version = %s FOR UPDATE",
                (version,),
            )
            existing = cur.fetchone()
            if not existing:
                raise ValueError(f"unknown release candidate: {version}")
            if existing[0] != "dev_verified":
                raise ValueError("release_not_dev_verified")
            if existing[1]:
                if existing[1] == signature and (existing[2] or "") == signing_key_id:
                    cur.execute(
                        "SELECT version, git_sha, modules, migration_from, migration_to, "
                        "security_notes, rollback_plan, status, created_at, images, rollback_kind, "
                        "signature, signing_key_id FROM control_release_manifests WHERE version = %s",
                        (version,),
                    )
                    return self._release(cur.fetchone())
                raise ValueError("production_signature_already_attached")
            cur.execute(
                "UPDATE control_release_manifests SET signature = %s, signing_key_id = %s "
                "WHERE version = %s RETURNING version, git_sha, modules, migration_from, migration_to, "
                "security_notes, rollback_plan, status, created_at, images, rollback_kind, signature, signing_key_id",
                (signature, signing_key_id, version),
            )
            row = cur.fetchone()
            self._insert_promotion_event(cur, ReleasePromotionEvent(
                id="", release_version=version, actor=actor,
                action="production_signature_attached", from_state="dev_verified",
                to_state="dev_verified", metadata={"signing_key_id": signing_key_id},
            ))
            conn.commit()
        return self._release(row)

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
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE control_release_promotions SET state = 'customer_approved', "
                "customer_approved_at = now(), customer_approved_by = %s, "
                "failure_reason = '', updated_at = now() "
                "WHERE release_version = %s AND state = 'dev_verified' "
                "AND EXISTS (SELECT 1 FROM control_release_manifests AS manifest "
                "WHERE manifest.version = release_version AND manifest.signature = %s "
                "AND manifest.signing_key_id = %s) "
                f"RETURNING {self._PROMOTION_COLS}",
                (actor, version, signature, signing_key_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("release must be dev_verified before customer approval")
            cur.execute(
                "UPDATE control_release_manifests SET status = 'active', signature = %s, signing_key_id = %s "
                "WHERE version = %s",
                (signature, signing_key_id, version),
            )
            self._insert_promotion_event(cur, ReleasePromotionEvent(
                id="", release_version=version, actor=actor, action="customer_approved",
                from_state="dev_verified", to_state="customer_approved", note=note,
                metadata={"signing_key_id": signing_key_id},
            ))
            conn.commit()
        return self._promotion(row)

    def get_release_gate(self) -> Optional[CustomerDeployment]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._DEPLOYMENT_COLS} FROM control_deployments "
                "WHERE is_release_gate = true AND status = 'active' AND removed_at IS NULL LIMIT 1"
            )
            row = cur.fetchone()
        return self._deployment(row) if row else None

    def designate_release_gate(self, deployment_id: str) -> CustomerDeployment:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT environment, deployment_type, status FROM control_deployments "
                "WHERE id = %s FOR UPDATE",
                (deployment_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown deployment: {deployment_id}")
            if row[0] != "development" or row[1] != "dedicated_server":
                raise ValueError("release gate must be a dedicated development server")
            if row[2] != "active":
                raise ValueError("release gate must be active")
            cur.execute("UPDATE control_deployments SET is_release_gate = false WHERE is_release_gate = true")
            cur.execute(
                "UPDATE control_deployments SET is_release_gate = true WHERE id = %s "
                f"RETURNING {self._DEPLOYMENT_COLS}",
                (deployment_id,),
            )
            updated = cur.fetchone()
            conn.commit()
        return self._deployment(updated)

    def update_deployment_telemetry(
        self,
        deployment_id: str,
        *,
        heartbeat_at: str,
        healthy: bool,
        reported_version: str = "",
        reported_migration: str = "",
    ) -> CustomerDeployment:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_deployments SET last_heartbeat_at = %s::timestamptz, "
                "last_heartbeat_healthy = %s, last_reported_version = %s, "
                "last_reported_migration = %s WHERE id = %s "
                f"RETURNING {self._DEPLOYMENT_COLS}",
                (
                    heartbeat_at, healthy, reported_version, reported_migration,
                    deployment_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown deployment: {deployment_id}")
            conn.commit()
        return self._deployment(row)

    def mark_deployment_provisioned(
        self,
        deployment_id: str,
        *,
        installed_at: str,
        version: str,
        migration: str = "",
    ) -> CustomerDeployment:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_deployments SET current_version = COALESCE(NULLIF(%s, ''), current_version), "
                "current_migration = COALESCE(NULLIF(%s, ''), current_migration), "
                "current_version_deployed_at = %s::timestamptz WHERE id = %s "
                f"RETURNING {self._DEPLOYMENT_COLS}",
                (version, migration, installed_at, deployment_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown deployment: {deployment_id}")
            conn.commit()
        return self._deployment(row)

    @staticmethod
    def _insert_promotion_event(cur, event: ReleasePromotionEvent) -> None:
        cur.execute(
            "INSERT INTO control_release_promotion_events "
            "(release_version, actor, action, from_state, to_state, note, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                event.release_version, event.actor, event.action, event.from_state,
                event.to_state, event.note, json.dumps(event.metadata),
            ),
        )

    def record_backup(self, backup: BackupRun) -> BackupRun:
        validate_run_status(backup.status)
        if not self.get_deployment(backup.deployment_id):
            raise ValueError(f"unknown deployment: {backup.deployment_id}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_backups (id, deployment_id, status, detail, created_at)
                VALUES (%s, %s, %s, %s, COALESCE(NULLIF(%s, '')::timestamptz, now()))
                RETURNING id, deployment_id, status, detail, created_at
                """,
                (backup.id, backup.deployment_id, backup.status, backup.detail, backup.created_at),
            )
            row = cur.fetchone()
            conn.commit()
        return self._backup(row)

    def latest_backup(self, deployment_id: str) -> Optional[BackupRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, deployment_id, status, detail, created_at
                FROM control_backups
                WHERE deployment_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (deployment_id,),
            )
            row = cur.fetchone()
        return self._backup(row) if row else None

    def record_health(self, health: HealthCheckRun) -> HealthCheckRun:
        validate_run_status(health.status)
        if not self.get_deployment(health.deployment_id):
            raise ValueError(f"unknown deployment: {health.deployment_id}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_health_checks (id, deployment_id, status, detail)
                VALUES (%s, %s, %s, %s)
                RETURNING id, deployment_id, status, detail, created_at
                """,
                (health.id, health.deployment_id, health.status, health.detail),
            )
            row = cur.fetchone()
            conn.commit()
        return self._health(row)

    def latest_health(self, deployment_id: str) -> Optional[HealthCheckRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, deployment_id, status, detail, created_at
                FROM control_health_checks
                WHERE deployment_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (deployment_id,),
            )
            row = cur.fetchone()
        return self._health(row) if row else None

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
            latest_backup=lambda: self.latest_backup(deployment_id),  # lazy (A3): the callable only runs
                                                                      # inside the backup gate, keeping the
                                                                      # extra SELECT off every plan_update
            ack_restore_required=ack_restore_required,
            require_signed_release=require_signed_releases(),
            promotion=promotion,
            gate_deployment_id=gate.id if gate else "",
            active_rollout=bool(
                (active := self.list_active_rollout(deployment_id))
                and active.id != ignore_rollout_id
            ),
            **release_promotion_plan_context(release, promotion, deployment),
        )

    def start_rollout(self, rollout: RolloutRun) -> RolloutRun:
        validate_run_status(rollout.status)
        if rollout.status == "success":
            raise ValueError("rollout cannot start as success")
        plan = self.plan_update(rollout.deployment_id, rollout.target_version,
                                ack_restore_required=rollout.ack_restore_required)
        if not plan.allowed:
            raise ValueError(f"rollout blocked: {plan.reason}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO control_rollouts
                (id, deployment_id, target_version, status, started_by, notes,
                 external_provider, ack_restore_required, fleet_rollout_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {self._ROLLOUT_COLS}
                """,
                (
                    rollout.id,
                    rollout.deployment_id,
                    rollout.target_version,
                    rollout.status,
                    rollout.started_by,
                    rollout.notes,
                    # Pin the provider on every write instead of inheriting a
                    # legacy database default from the retired workflow era.
                    rollout.external_provider,
                    rollout.ack_restore_required,
                    # Fleet children link back to their fleet rollout here; the
                    # memory store persists the whole dataclass, and
                    # list_rollouts_for_fleet is keyed on this column — dropping
                    # it would make ring reconciliation count zero children.
                    rollout.fleet_rollout_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._rollout(row)

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "", apply: bool = True) -> RolloutRun:
        validate_run_status(status)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts WHERE id = %s",
                (rollout_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown rollout: {rollout_id}")
            rollout = self._rollout(row)
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
                    cur.execute(
                        """
                        UPDATE control_deployment_modules
                        SET version = %s, updated_at = now()
                        WHERE deployment_id = %s AND module_id = %s
                        """,
                        (release.modules[module.module_id], module.deployment_id, module.module_id),
                    )
                cur.execute(
                    """
                    UPDATE control_deployments
                    SET current_version = %s,
                        current_migration = %s,
                        current_version_deployed_at = now()
                    WHERE id = %s
                    """,
                    (
                        release.version,
                        release.migration_to or deployment.current_migration,
                        deployment.id,
                    ),
                )

            cur.execute(
                f"""
                UPDATE control_rollouts
                SET status = %s, notes = %s, updated_at = now()
                WHERE id = %s
                RETURNING {self._ROLLOUT_COLS}
                """,
                (updated.status, updated.notes, rollout_id),
            )
            updated_row = cur.fetchone()
            conn.commit()
        return self._rollout(updated_row)

    def complete_verified_rollout(
        self,
        rollout_id: str,
        *,
        verified_modules: Dict[str, str],
        completed_at: str,
    ) -> RolloutRun:
        """Atomically apply authenticated module evidence and complete a pull rollout."""
        from app.controlplane.development_gate import validate_module_transition

        verified = {
            str(module_id).strip(): str(version).strip()
            for module_id, version in verified_modules.items()
        }
        finished_at = completed_at.strip() or datetime.now(timezone.utc).isoformat()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts WHERE id = %s FOR UPDATE",
                (rollout_id,),
            )
            locked = cur.fetchone()
            if not locked:
                raise ValueError(f"unknown rollout: {rollout_id}")
            current_rollout = self._rollout(locked)
            if current_rollout.status in {"success", "failed"}:
                raise ValueError("terminal rollout status cannot be changed")
            cur.execute(
                f"SELECT {self._PROMOTION_COLS} FROM control_release_promotions "
                "WHERE release_version = %s FOR SHARE",
                (current_rollout.target_version,),
            )
            promotion_row = cur.fetchone()
            cur.execute(
                """
                SELECT version, git_sha, modules, migration_from, migration_to,
                    security_notes, rollback_plan, status, created_at, images,
                    rollback_kind, signature, signing_key_id
                FROM control_release_manifests
                WHERE version = %s
                FOR SHARE
                """,
                (current_rollout.target_version,),
            )
            release_row = cur.fetchone()
            cur.execute(
                f"SELECT {self._DEPLOYMENT_COLS} FROM control_deployments "
                "WHERE id = %s FOR UPDATE",
                (current_rollout.deployment_id,),
            )
            deployment_row = cur.fetchone()
            if not release_row or not deployment_row:
                raise ValueError("rollout target is no longer available")
            release = self._release(release_row)
            deployment = self._deployment(deployment_row)
            if verified != release.modules:
                raise ValueError("verified rollout modules do not match release")
            cur.execute(
                """
                SELECT deployment_id, module_id, version, status
                FROM control_deployment_modules
                WHERE deployment_id = %s
                ORDER BY module_id
                FOR UPDATE
                """,
                (deployment.id,),
            )
            current_modules = [self._module(row) for row in cur.fetchall()]
            promotion = self._promotion(promotion_row) if promotion_row else None

            if deployment.is_release_gate and deployment.status == "active":
                gate_deployment_id = deployment.id
            else:
                cur.execute(
                    "SELECT id FROM control_deployments "
                    "WHERE is_release_gate = true AND status = 'active' LIMIT 1 FOR SHARE"
                )
                gate_row = cur.fetchone()
                gate_deployment_id = str(gate_row[0]) if gate_row else ""

            cur.execute(
                "SELECT 1 FROM control_rollouts "
                "WHERE deployment_id = %s AND id <> %s "
                "AND status NOT IN ('success', 'failed') LIMIT 1 FOR SHARE",
                (deployment.id, current_rollout.id),
            )
            active_rollout = cur.fetchone() is not None

            def latest_locked_backup():
                cur.execute(
                    "SELECT id, deployment_id, status, detail, created_at "
                    "FROM control_backups WHERE deployment_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT 1 FOR SHARE",
                    (deployment.id,),
                )
                backup_row = cur.fetchone()
                return self._backup(backup_row) if backup_row else None

            plan = compute_update_plan(
                deployment.id,
                current_rollout.target_version,
                deployment=deployment,
                release=release,
                modules=current_modules,
                latest_backup=latest_locked_backup,
                ack_restore_required=current_rollout.ack_restore_required,
                require_signed_release=require_signed_releases(),
                promotion=promotion,
                gate_deployment_id=gate_deployment_id,
                active_rollout=active_rollout,
                **release_promotion_plan_context(release, promotion, deployment),
            )
            if not plan.allowed:
                raise ValueError(f"rollout completion blocked: {plan.reason}")

            if deployment.is_release_gate:
                current = {
                    module.module_id
                    for module in current_modules
                    if module.status == "active"
                }
                reason = validate_module_transition(current, verified)
                if reason:
                    raise ValueError(reason)

            for module_id, version in verified.items():
                cur.execute(
                    """
                    INSERT INTO control_deployment_modules
                    (deployment_id, module_id, version, status)
                    VALUES (%s, %s, %s, 'active')
                    ON CONFLICT (deployment_id, module_id) DO UPDATE SET
                        version = EXCLUDED.version,
                        status = 'active',
                        updated_at = now()
                    """,
                    (deployment.id, module_id, version),
                )
            cur.execute(
                """
                UPDATE control_deployments
                SET current_version = %s,
                    current_migration = %s,
                    current_version_deployed_at = %s
                WHERE id = %s
                """,
                (
                    release.version,
                    release.migration_to or deployment.current_migration,
                    finished_at,
                    deployment.id,
                ),
            )
            cur.execute(
                f"""
                UPDATE control_rollouts
                SET status = 'success',
                    exec_status = 'succeeded',
                    failure_reason = '',
                    completed_at = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING {self._ROLLOUT_COLS}
                """,
                (finished_at, rollout_id),
            )
            updated_row = cur.fetchone()
            conn.commit()
        return self._rollout(updated_row)

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts "
                "WHERE deployment_id = %s ORDER BY created_at DESC, id DESC",
                (deployment_id,),
            )
            rows = cur.fetchall()
        return [self._rollout(row) for row in rows]

    _ROLLOUT_COLS = (
        "id, deployment_id, target_version, status, started_by, notes, created_at, "
        "exec_status, external_provider, external_run_id, external_run_url, "
        "failure_reason, request_payload, dispatched_at, completed_at, fleet_rollout_id, "
        "ack_restore_required"
    )
    _EXEC_FIELDS = {
        "exec_status", "external_run_id", "external_run_url", "failure_reason",
        "dispatched_at", "completed_at", "request_payload", "fleet_rollout_id",
    }

    _TEARDOWN_REQUEST_COLS = (
        "id, deployment_id, account_id, nonce_hash, nonce_expires_at, "
        "legal_hold_evidence_ref, backup_retention_evidence_ref, requested_by, "
        "approver_ids, status, execution_result, created_at, updated_at, completed_at"
    )

    def get_rollout(self, rollout_id: str) -> Optional[RolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts WHERE id = %s", (rollout_id,))
            row = cur.fetchone()
        return self._rollout(row) if row else None

    def list_active_rollout(self, deployment_id: str) -> Optional[RolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts "
                "WHERE deployment_id = %s AND status NOT IN ('success', 'failed') "
                "ORDER BY created_at, id LIMIT 1",
                (deployment_id,),
            )
            row = cur.fetchone()
        return self._rollout(row) if row else None

    def list_rollouts_for_fleet(self, fleet_rollout_id: str) -> List[RolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts "
                "WHERE fleet_rollout_id = %s ORDER BY created_at, id",
                (fleet_rollout_id,),
            )
            return [self._rollout(row) for row in cur.fetchall()]

    def claim_rollout_dispatch(self, rollout_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_rollouts SET exec_status = 'dispatched', updated_at = now() "
                "WHERE id = %s AND exec_status = 'pending'",
                (rollout_id,),
            )
            claimed = cur.rowcount == 1
            conn.commit()
        return claimed

    def update_rollout_exec(self, rollout_id: str, **fields) -> RolloutRun:
        bad = set(fields) - self._EXEC_FIELDS
        if bad:
            raise ValueError(f"cannot update rollout exec fields: {sorted(bad)}")
        if not fields:
            got = self.get_rollout(rollout_id)
            if not got:
                raise ValueError(f"unknown rollout: {rollout_id}")
            return got
        sets, params = [], []
        for key, value in fields.items():
            if key == "request_payload":
                sets.append("request_payload = %s::jsonb")
                params.append(json.dumps(value))
            elif key in ("dispatched_at", "completed_at"):
                sets.append(f"{key} = %s::timestamptz")
                params.append(value or None)
            else:
                sets.append(f"{key} = %s")
                params.append(value)
        params.append(rollout_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE control_rollouts SET {', '.join(sets)}, updated_at = now() "
                f"WHERE id = %s RETURNING {self._ROLLOUT_COLS}",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown rollout: {rollout_id}")
            conn.commit()
        return self._rollout(row)

    # --- fleet rollouts (Phase 2 orchestration) ---
    _FLEET_COLS = ("id, target_version, git_sha, status, ring_order, current_ring, "
                   "failure_tolerance, started_by, notes, created_at, updated_at, callback_url, dry_run, "
                   "ring_batch_size, only_deployment_ids, include_manual_pinned")

    def _fleet_rollout(self, row) -> FleetRolloutRun:
        ring_order = row[4]
        if isinstance(ring_order, str):
            ring_order = json.loads(ring_order) if ring_order else []
        only_deployment_ids = row[14] if len(row) > 14 else []
        if isinstance(only_deployment_ids, str):
            only_deployment_ids = json.loads(only_deployment_ids) if only_deployment_ids else []
        return FleetRolloutRun(
            id=row[0], target_version=row[1], git_sha=row[2] or "", status=row[3],
            ring_order=tuple(ring_order or ()), current_ring=row[5] or "",
            failure_tolerance=int(row[6]), started_by=row[7] or "", notes=row[8] or "",
            created_at=_iso(row[9]), updated_at=_iso(row[10]), callback_url=row[11] or "", dry_run=bool(row[12]),
            ring_batch_size=int(row[13]) if len(row) > 13 else 1,
            only_deployment_ids=tuple(only_deployment_ids or ()),
            include_manual_pinned=bool(row[15]) if len(row) > 15 else False,
        )

    def create_fleet_rollout(self, fleet_run: FleetRolloutRun) -> FleetRolloutRun:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO control_fleet_rollouts "
                "(id, target_version, git_sha, status, ring_order, current_ring, "
                " failure_tolerance, started_by, notes, callback_url, dry_run, ring_batch_size, "
                " only_deployment_ids, include_manual_pinned) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) "
                f"RETURNING {self._FLEET_COLS}",
                (fleet_run.id, fleet_run.target_version, fleet_run.git_sha, fleet_run.status,
                 json.dumps(list(fleet_run.ring_order)), fleet_run.current_ring,
                 fleet_run.failure_tolerance, fleet_run.started_by, fleet_run.notes,
                 fleet_run.callback_url, fleet_run.dry_run, fleet_run.ring_batch_size,
                 json.dumps(list(fleet_run.only_deployment_ids)), fleet_run.include_manual_pinned),
            )
            row = cur.fetchone()
            conn.commit()
        return self._fleet_rollout(row)

    def get_fleet_rollout(self, fleet_rollout_id: str) -> Optional[FleetRolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._FLEET_COLS} FROM control_fleet_rollouts WHERE id = %s", (fleet_rollout_id,))
            row = cur.fetchone()
        return self._fleet_rollout(row) if row else None

    def list_fleet_rollouts(self) -> List[FleetRolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._FLEET_COLS} FROM control_fleet_rollouts ORDER BY updated_at DESC, id DESC")
            return [self._fleet_rollout(r) for r in cur.fetchall()]

    def update_fleet_rollout(self, fleet_rollout_id: str, **fields) -> FleetRolloutRun:
        bad = set(fields) - FLEET_EXEC_FIELDS
        if bad:
            raise ValueError(f"cannot update fleet rollout fields: {sorted(bad)}")
        if not fields:
            got = self.get_fleet_rollout(fleet_rollout_id)
            if not got:
                raise ValueError(f"unknown fleet rollout: {fleet_rollout_id}")
            return got
        sets, params = [], []
        for key, value in fields.items():
            if key == "ring_order":
                sets.append("ring_order = %s::jsonb")
                params.append(json.dumps(list(value)))
            else:
                sets.append(f"{key} = %s")
                params.append(value)
        params.append(fleet_rollout_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE control_fleet_rollouts SET {', '.join(sets)}, updated_at = now() "
                f"WHERE id = %s RETURNING {self._FLEET_COLS}",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown fleet rollout: {fleet_rollout_id}")
            conn.commit()
        return self._fleet_rollout(row)

    def advance_fleet_ring(self, fleet_rollout_id: str, from_ring: str, to_ring: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_fleet_rollouts SET current_ring = %s, updated_at = now() "
                "WHERE id = %s AND current_ring = %s AND status = 'running'",
                (to_ring, fleet_rollout_id, from_ring),
            )
            claimed = cur.rowcount == 1
            conn.commit()
        return claimed

    # --- served floor bumps (P5-01) ---
    def set_served_floor_bump(self, bump: ServedFloorBump) -> ServedFloorBump:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_served_floor_bumps (scope, bump_json, floor_version, updated_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (scope) DO UPDATE SET
                    bump_json = EXCLUDED.bump_json,
                    floor_version = EXCLUDED.floor_version,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                RETURNING scope, bump_json, floor_version, updated_by, updated_at
                """,
                (bump.scope, bump.bump_json, bump.floor_version, bump.updated_by),
            )
            row = cur.fetchone()
            conn.commit()
        return self._served_floor_bump(row)

    def clear_served_floor_bump(self, scope: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM control_served_floor_bumps WHERE scope = %s", (scope,))
            deleted = cur.rowcount == 1
            conn.commit()
        return deleted

    def get_served_floor_bump(self, scope: str) -> Optional[ServedFloorBump]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT scope, bump_json, floor_version, updated_by, updated_at "
                "FROM control_served_floor_bumps WHERE scope = %s",
                (scope,),
            )
            row = cur.fetchone()
        return self._served_floor_bump(row) if row else None

    def list_served_floor_bumps(self) -> List[ServedFloorBump]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT scope, bump_json, floor_version, updated_by, updated_at "
                "FROM control_served_floor_bumps ORDER BY scope"
            )
            rows = cur.fetchall()
        return [self._served_floor_bump(row) for row in rows]

    # --- customer teardown protocol (record-only; no execution capability) ---
    def create_teardown_request(self, request: CustomerTeardownRequest) -> CustomerTeardownRequest:
        validate_teardown_request(request)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT account_id FROM control_deployments WHERE id = %s FOR KEY SHARE",
                (request.deployment_id,),
            )
            deployment = cur.fetchone()
            if not deployment:
                raise ValueError(f"unknown deployment: {request.deployment_id}")
            if deployment[0] and deployment[0] != request.account_id:
                raise ValueError("teardown request account does not match deployment ownership.")
            cur.execute(
                f"""
                INSERT INTO control_customer_teardown_requests
                (id, deployment_id, account_id, nonce_hash, nonce_expires_at,
                 legal_hold_evidence_ref, backup_retention_evidence_ref, requested_by,
                 approver_ids, status, execution_result, created_at, updated_at, completed_at)
                VALUES (%s, %s, %s, %s, %s::timestamptz, %s, %s, %s,
                        %s::jsonb, %s, %s, COALESCE(%s::timestamptz, now()),
                        COALESCE(%s::timestamptz, now()), %s::timestamptz)
                RETURNING {self._TEARDOWN_REQUEST_COLS}
                """,
                (
                    request.id,
                    request.deployment_id,
                    request.account_id,
                    request.nonce_hash,
                    request.nonce_expires_at,
                    request.legal_hold_evidence_ref,
                    request.backup_retention_evidence_ref,
                    request.requested_by,
                    json.dumps(list(request.approver_ids)),
                    request.status,
                    request.execution_result,
                    request.created_at or None,
                    request.updated_at or request.created_at or None,
                    request.completed_at or None,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._teardown_request(row)

    def get_teardown_request(self, request_id: str) -> Optional[CustomerTeardownRequest]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._TEARDOWN_REQUEST_COLS} "
                "FROM control_customer_teardown_requests WHERE id = %s",
                (request_id,),
            )
            row = cur.fetchone()
        return self._teardown_request(row) if row else None

    def list_teardown_requests(self, deployment_id: str) -> List[CustomerTeardownRequest]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._TEARDOWN_REQUEST_COLS} "
                "FROM control_customer_teardown_requests "
                "WHERE deployment_id = %s ORDER BY created_at, id",
                (deployment_id,),
            )
            rows = cur.fetchall()
        return [self._teardown_request(row) for row in rows]

    def approve_teardown_request(
        self,
        request_id: str,
        *,
        approver_id: str,
        nonce_hash: str,
        approved_at: str,
    ) -> CustomerTeardownRequest:
        # The row lock makes the two-person rule race-safe: two concurrent
        # approvals serialize before either can append an approver identity.
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._TEARDOWN_REQUEST_COLS} "
                "FROM control_customer_teardown_requests WHERE id = %s FOR UPDATE",
                (request_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("teardown request not found.")
            updated = apply_teardown_approval(
                self._teardown_request(row),
                approver_id=approver_id,
                nonce_hash=nonce_hash,
                approved_at=approved_at,
            )
            cur.execute(
                f"""
                UPDATE control_customer_teardown_requests
                SET approver_ids = %s::jsonb,
                    status = %s,
                    execution_result = %s,
                    updated_at = %s::timestamptz,
                    completed_at = %s::timestamptz
                WHERE id = %s
                RETURNING {self._TEARDOWN_REQUEST_COLS}
                """,
                (
                    json.dumps(list(updated.approver_ids)),
                    updated.status,
                    updated.execution_result,
                    updated.updated_at or approved_at,
                    updated.completed_at or None,
                    request_id,
                ),
            )
            stored = cur.fetchone()
            conn.commit()
        return self._teardown_request(stored)

    def record_teardown_execution(
        self,
        request_id: str,
        *,
        succeeded: bool,
        result: str,
        executed_at: str,
    ) -> CustomerTeardownRequest:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._TEARDOWN_REQUEST_COLS} "
                "FROM control_customer_teardown_requests WHERE id = %s FOR UPDATE",
                (request_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("teardown request not found.")
            updated = apply_teardown_execution(
                self._teardown_request(row),
                succeeded=succeeded,
                result=result,
                executed_at=executed_at,
            )
            cur.execute(
                f"""
                UPDATE control_customer_teardown_requests
                SET status = %s,
                    execution_result = %s,
                    updated_at = %s::timestamptz,
                    completed_at = %s::timestamptz
                WHERE id = %s
                RETURNING {self._TEARDOWN_REQUEST_COLS}
                """,
                (
                    updated.status,
                    updated.execution_result,
                    updated.updated_at or executed_at,
                    updated.completed_at or None,
                    request_id,
                ),
            )
            stored = cur.fetchone()
            conn.commit()
        return self._teardown_request(stored)

    def _served_floor_bump(self, row) -> ServedFloorBump:
        return ServedFloorBump(
            scope=row[0],
            bump_json=row[1],
            floor_version=row[2] or "",
            updated_by=row[3] or "",
            updated_at=_iso(row[4]),
        )

    def _teardown_request(self, row) -> CustomerTeardownRequest:
        approver_ids = row[8]
        if isinstance(approver_ids, str):
            approver_ids = json.loads(approver_ids or "[]")
        return CustomerTeardownRequest(
            id=row[0],
            deployment_id=row[1],
            account_id=row[2],
            nonce_hash=row[3],
            nonce_expires_at=_iso(row[4]),
            legal_hold_evidence_ref=row[5],
            backup_retention_evidence_ref=row[6],
            requested_by=row[7],
            approver_ids=tuple(approver_ids or ()),
            status=row[9],
            execution_result=row[10] or "",
            created_at=_iso(row[11]),
            updated_at=_iso(row[12]),
            completed_at=_iso(row[13]),
        )

    def _deployment(self, row) -> CustomerDeployment:
        return CustomerDeployment(
            id=row[0],
            customer_name=row[1],
            environment=row[2],
            deployment_type=row[3],
            region=row[4],
            release_ring=row[5],
            status=row[6],
            current_version=row[7],
            current_migration=row[8],
            created_at=_iso(row[9]),
            account_id=row[10] or "",
            update_policy=row[11] or "",
            is_release_gate=bool(row[12]) if len(row) > 12 else False,
            current_version_deployed_at=_iso(row[13]) if len(row) > 13 else "",
            last_heartbeat_at=_iso(row[14]) if len(row) > 14 else "",
            last_heartbeat_healthy=(
                None if len(row) <= 15 or row[15] is None else bool(row[15])
            ),
            last_reported_version=(row[16] or "") if len(row) > 16 else "",
            last_reported_migration=(row[17] or "") if len(row) > 17 else "",
            selected_module_ids=_json_list(row[18]) if len(row) > 18 else (),
            removed_at=_iso(row[19]) if len(row) > 19 else "",
        )

    def _promotion(self, row) -> ReleasePromotion:
        return ReleasePromotion(
            release_version=row[0], state=row[1], gate_deployment_id=row[2] or "",
            dev_signature=row[3] or "", dev_signing_key_id=row[4] or "",
            dev_rollout_id=row[5] or "", dev_attempt_id=row[6] or "",
            dev_started_at=_iso(row[7]), dev_completed_at=_iso(row[8]),
            dev_verified_at=_iso(row[9]), customer_approved_at=_iso(row[10]),
            customer_approved_by=row[11] or "", customer_paused_at=_iso(row[12]),
            customer_paused_reason=row[13] or "", yanked_at=_iso(row[14]),
            failure_reason=row[15] or "", created_at=_iso(row[16]), updated_at=_iso(row[17]),
        )

    def _promotion_event(self, row) -> ReleasePromotionEvent:
        metadata = row[7]
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        return ReleasePromotionEvent(
            id=str(row[0]), release_version=row[1], actor=row[2] or "", action=row[3],
            from_state=row[4] or "", to_state=row[5], note=row[6] or "",
            metadata=metadata or {}, created_at=_iso(row[8]),
        )

    def _module(self, row) -> DeploymentModule:
        return DeploymentModule(deployment_id=row[0], module_id=row[1], version=row[2], status=row[3])

    def _release(self, row) -> ReleaseManifest:
        return ReleaseManifest(
            version=row[0],
            git_sha=row[1],
            modules=_json_dict(row[2]),
            migration_from=row[3],
            migration_to=row[4],
            security_notes=row[5],
            rollback_plan=row[6],
            status=row[7],
            created_at=_iso(row[8]),
            images=_json_dict(row[9]),
            rollback_kind=row[10] or "",
            signature=row[11] or "",
            signing_key_id=row[12] or "",
        )

    def _backup(self, row) -> BackupRun:
        return BackupRun(id=row[0], deployment_id=row[1], status=row[2], detail=row[3], created_at=_iso(row[4]))

    def _health(self, row) -> HealthCheckRun:
        return HealthCheckRun(id=row[0], deployment_id=row[1], status=row[2], detail=row[3], created_at=_iso(row[4]))

    def _rollout(self, row) -> RolloutRun:
        payload = row[12]
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        return RolloutRun(
            id=row[0],
            deployment_id=row[1],
            target_version=row[2],
            status=row[3],
            started_by=row[4],
            notes=row[5],
            created_at=_iso(row[6]),
            exec_status=row[7],
            external_provider=row[8],
            external_run_id=row[9] or "",
            external_run_url=row[10] or "",
            failure_reason=row[11] or "",
            request_payload=payload or {},
            dispatched_at=_iso(row[13]),
            completed_at=_iso(row[14]),
            fleet_rollout_id=row[15] or "",
            ack_restore_required=bool(row[16]),
        )

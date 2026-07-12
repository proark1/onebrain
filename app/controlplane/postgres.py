"""Postgres-backed operator control-plane store."""

from __future__ import annotations

import json
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
    ServedFloorBump,
    UpdatePlan,
    compute_update_plan,
    require_signed_releases,
    validate_deployment,
    validate_module,
    validate_release,
    validate_run_status,
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
                ),
            )

    def create_deployment(self, deployment: CustomerDeployment) -> CustomerDeployment:
        validate_deployment(deployment)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_deployments
                (id, customer_name, environment, deployment_type, region, release_ring,
                 status, current_version, current_migration, account_id, update_policy)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, customer_name, environment, deployment_type, region,
                    release_ring, status, current_version, current_migration, created_at, account_id, update_policy
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
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._deployment(row)

    def get_deployment(self, deployment_id: str) -> Optional[CustomerDeployment]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, customer_name, environment, deployment_type, region,
                    release_ring, status, current_version, current_migration, created_at, account_id, update_policy
                FROM control_deployments
                WHERE id = %s
                """,
                (deployment_id,),
            )
            row = cur.fetchone()
        return self._deployment(row) if row else None

    def list_deployments(self) -> List[CustomerDeployment]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, customer_name, environment, deployment_type, region,
                    release_ring, status, current_version, current_migration, created_at, account_id, update_policy
                FROM control_deployments
                ORDER BY lower(customer_name), id
                """
            )
            rows = cur.fetchall()
        return [self._deployment(row) for row in rows]

    def set_update_policy(self, deployment_id: str, update_policy: str) -> CustomerDeployment:
        if update_policy not in UPDATE_POLICIES or not update_policy:
            raise ValueError(f"Unknown update policy: {update_policy}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE control_deployments SET update_policy = %s WHERE id = %s "
                "RETURNING id, customer_name, environment, deployment_type, region, "
                "release_ring, status, current_version, current_migration, created_at, account_id, update_policy",
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
                ORDER BY version
                """
            )
            rows = cur.fetchall()
        return [self._release(row) for row in rows]

    def record_backup(self, backup: BackupRun) -> BackupRun:
        validate_run_status(backup.status)
        if not self.get_deployment(backup.deployment_id):
            raise ValueError(f"unknown deployment: {backup.deployment_id}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_backups (id, deployment_id, status, detail)
                VALUES (%s, %s, %s, %s)
                RETURNING id, deployment_id, status, detail, created_at
                """,
                (backup.id, backup.deployment_id, backup.status, backup.detail),
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

    def plan_update(self, deployment_id: str, target_version: str, *, ack_restore_required: bool = False) -> UpdatePlan:
        deployment = self.get_deployment(deployment_id)
        return compute_update_plan(
            deployment_id, target_version,
            deployment=deployment,
            release=self.get_release(target_version),
            modules=self.list_modules(deployment_id) if deployment else [],
            latest_backup=lambda: self.latest_backup(deployment_id),  # lazy (A3): the callable only runs
                                                                      # inside the backup gate, keeping the
                                                                      # extra SELECT off every plan_update
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
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO control_rollouts
                (id, deployment_id, target_version, status, started_by, notes,
                 ack_restore_required, fleet_rollout_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {self._ROLLOUT_COLS}
                """,
                (
                    rollout.id,
                    rollout.deployment_id,
                    rollout.target_version,
                    rollout.status,
                    rollout.started_by,
                    rollout.notes,
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
                        current_migration = %s
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

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ROLLOUT_COLS} FROM control_rollouts "
                "WHERE deployment_id = %s ORDER BY created_at, id",
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
                   "failure_tolerance, started_by, notes, created_at, callback_url, dry_run")

    def _fleet_rollout(self, row) -> FleetRolloutRun:
        ring_order = row[4]
        if isinstance(ring_order, str):
            ring_order = json.loads(ring_order) if ring_order else []
        return FleetRolloutRun(
            id=row[0], target_version=row[1], git_sha=row[2] or "", status=row[3],
            ring_order=tuple(ring_order or ()), current_ring=row[5] or "",
            failure_tolerance=int(row[6]), started_by=row[7] or "", notes=row[8] or "",
            created_at=_iso(row[9]), callback_url=row[10] or "", dry_run=bool(row[11]),
        )

    def create_fleet_rollout(self, fleet_run: FleetRolloutRun) -> FleetRolloutRun:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO control_fleet_rollouts "
                "(id, target_version, git_sha, status, ring_order, current_ring, "
                " failure_tolerance, started_by, notes, callback_url, dry_run) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {self._FLEET_COLS}",
                (fleet_run.id, fleet_run.target_version, fleet_run.git_sha, fleet_run.status,
                 json.dumps(list(fleet_run.ring_order)), fleet_run.current_ring,
                 fleet_run.failure_tolerance, fleet_run.started_by, fleet_run.notes,
                 fleet_run.callback_url, fleet_run.dry_run),
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
            cur.execute(f"SELECT {self._FLEET_COLS} FROM control_fleet_rollouts ORDER BY created_at, id")
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

    def _served_floor_bump(self, row) -> ServedFloorBump:
        return ServedFloorBump(
            scope=row[0],
            bump_json=row[1],
            floor_version=row[2] or "",
            updated_by=row[3] or "",
            updated_at=_iso(row[4]),
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

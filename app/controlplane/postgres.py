"""Postgres-backed operator control-plane store."""

from __future__ import annotations

import json
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
                ),
            )

    def create_deployment(self, deployment: CustomerDeployment) -> CustomerDeployment:
        validate_deployment(deployment)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_deployments
                (id, customer_name, environment, deployment_type, region, release_ring,
                 status, current_version, current_migration, account_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, customer_name, environment, deployment_type, region,
                    release_ring, status, current_version, current_migration, created_at, account_id
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
                    release_ring, status, current_version, current_migration, created_at, account_id
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
                    release_ring, status, current_version, current_migration, created_at, account_id
                FROM control_deployments
                ORDER BY lower(customer_name), id
                """
            )
            rows = cur.fetchall()
        return [self._deployment(row) for row in rows]

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
                 security_notes, rollback_plan, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING version, git_sha, modules, migration_from, migration_to,
                    security_notes, rollback_plan, status, created_at
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
                    security_notes, rollback_plan, status, created_at
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
                    security_notes, rollback_plan, status, created_at
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
                deployment_id,
                target_version,
                False,
                f"release_missing_modules:{','.join(missing)}",
                current_modules=current,
                target_modules=release.modules,
            )
        if release.migration_to and release.migration_to != deployment.current_migration:
            latest = self.latest_backup(deployment_id)
            if not latest or latest.status != "success":
                return UpdatePlan(
                    deployment_id,
                    target_version,
                    False,
                    "backup_required_for_schema_update",
                    current_modules=current,
                    target_modules=release.modules,
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
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_rollouts
                (id, deployment_id, target_version, status, started_by, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, deployment_id, target_version, status, started_by, notes, created_at
                """,
                (
                    rollout.id,
                    rollout.deployment_id,
                    rollout.target_version,
                    rollout.status,
                    rollout.started_by,
                    rollout.notes,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._rollout(row)

    def update_rollout_status(self, rollout_id: str, status: str, notes: str = "") -> RolloutRun:
        validate_run_status(status)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, deployment_id, target_version, status, started_by, notes, created_at
                FROM control_rollouts
                WHERE id = %s
                """,
                (rollout_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"unknown rollout: {rollout_id}")
            rollout = self._rollout(row)
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
                """
                UPDATE control_rollouts
                SET status = %s, notes = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, deployment_id, target_version, status, started_by, notes, created_at
                """,
                (updated.status, updated.notes, rollout_id),
            )
            updated_row = cur.fetchone()
            conn.commit()
        return self._rollout(updated_row)

    def list_rollouts(self, deployment_id: str) -> List[RolloutRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, deployment_id, target_version, status, started_by, notes, created_at
                FROM control_rollouts
                WHERE deployment_id = %s
                ORDER BY created_at, id
                """,
                (deployment_id,),
            )
            rows = cur.fetchall()
        return [self._rollout(row) for row in rows]

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
        )

    def _backup(self, row) -> BackupRun:
        return BackupRun(id=row[0], deployment_id=row[1], status=row[2], detail=row[3], created_at=_iso(row[4]))

    def _health(self, row) -> HealthCheckRun:
        return HealthCheckRun(id=row[0], deployment_id=row[1], status=row[2], detail=row[3], created_at=_iso(row[4]))

    def _rollout(self, row) -> RolloutRun:
        return RolloutRun(
            id=row[0],
            deployment_id=row[1],
            target_version=row[2],
            status=row[3],
            started_by=row[4],
            notes=row[5],
            created_at=_iso(row[6]),
        )

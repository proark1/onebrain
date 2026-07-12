from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.db.schema import (
    BASELINE_ALEMBIC_REVISION,
    MIGRATION_GUIDANCE,
    REQUIRED_ALEMBIC_REVISION,
    PostgresSchemaError,
    validate_postgres_schema,
)


class FakeCursor:
    def __init__(
        self,
        version: str = REQUIRED_ALEMBIC_REVISION,
        missing_tables: set[str] | None = None,
        missing_version_table: bool = False,
    ):
        self.version = version
        self.missing_tables = missing_tables or set()
        self.missing_version_table = missing_version_table
        self.last_sql = ""
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        if self.missing_version_table and "alembic_version" in sql:
            raise RuntimeError("relation alembic_version does not exist")

    def fetchone(self):
        if "alembic_version" in self.last_sql:
            return (self.version,)
        if "to_regclass" in self.last_sql:
            table = self.last_params[0]
            return (None,) if table in self.missing_tables else (table,)
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _baseline_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0001_baseline_onebrain_schema.py"
    )
    spec = importlib.util.spec_from_file_location("baseline_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _jobs_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0002_postgres_worker_jobs.py"
    )
    spec = importlib.util.spec_from_file_location("jobs_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _service_key_lifecycle_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0003_service_key_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("service_key_lifecycle_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _brand_theme_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0004_brand_theme_provisioning.py"
    )
    spec = importlib.util.spec_from_file_location("brand_theme_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _control_plane_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0005_control_plane_postgres.py"
    )
    spec = importlib.util.spec_from_file_location("control_plane_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _provisioning_runs_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0006_provisioning_runs.py"
    )
    spec = importlib.util.spec_from_file_location("provisioning_runs_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _governance_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0007_governance_privacy_retention.py"
    )
    spec = importlib.util.spec_from_file_location("governance_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_migration_module(filename: str, name: str):
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _rls_hardening_module():
    return _load_migration_module("0008_rls_hardening.py", "rls_hardening_migration")


def _rls_admin_role_module():
    return _load_migration_module("0009_rls_admin_role.py", "rls_admin_role_migration")


def _append_only_audit_module():
    return _load_migration_module("0010_append_only_audit.py", "append_only_audit_migration")


def test_validate_postgres_schema_requires_alembic_version_table():
    conn = FakeConnection(FakeCursor(missing_version_table=True))

    with pytest.raises(PostgresSchemaError, match="alembic upgrade head"):
        validate_postgres_schema(conn, ("users",))


def test_validate_postgres_schema_requires_current_head_revision():
    conn = FakeConnection(FakeCursor(version="old_revision"))

    with pytest.raises(PostgresSchemaError, match=REQUIRED_ALEMBIC_REVISION):
        validate_postgres_schema(conn, ("users",))


def test_validate_postgres_schema_reports_missing_tables():
    conn = FakeConnection(FakeCursor(missing_tables={"users"}))

    with pytest.raises(PostgresSchemaError, match="Missing tables: users"):
        validate_postgres_schema(conn, ("users",))


def test_baseline_migration_tracks_expected_tables_and_revision():
    migration = _baseline_migration_module()

    assert migration.revision == BASELINE_ALEMBIC_REVISION
    assert {
        "chunks",
        "users",
        "conversations",
        "messages",
        "service_keys",
        "platform_accounts",
        "platform_spaces",
        "platform_app_installations",
        "platform_audit_events",
        "intake_records",
    } == set(migration.BASELINE_TABLES)


def test_jobs_migration_tracks_expected_tables_and_revision():
    migration = _jobs_migration_module()

    assert migration.revision == "0002_postgres_worker_jobs"
    assert migration.down_revision == BASELINE_ALEMBIC_REVISION
    assert {"jobs", "job_files"} == set(migration.JOB_TABLES)


def test_service_key_lifecycle_migration_tracks_expected_head():
    migration = _service_key_lifecycle_migration_module()

    assert migration.revision == "0003_service_key_lifecycle"
    assert migration.down_revision == "0002_postgres_worker_jobs"
    assert {
        "last_used_at",
        "last_used_endpoint",
        "use_count",
        "rotated_from_id",
        "revoked_at",
    } == set(migration.SERVICE_KEY_LIFECYCLE_COLUMNS)


def test_brand_theme_migration_tracks_expected_head():
    migration = _brand_theme_migration_module()

    assert migration.revision == "0004_brand_theme_provisioning"
    assert migration.down_revision == "0003_service_key_lifecycle"
    assert {"platform_brand_themes"} == set(migration.BRAND_THEME_TABLES)


def test_control_plane_migration_tracks_expected_head():
    migration = _control_plane_migration_module()

    assert migration.revision == "0005_control_plane_postgres"
    assert migration.down_revision == "0004_brand_theme_provisioning"
    assert {
        "control_deployments",
        "control_deployment_modules",
        "control_release_manifests",
        "control_backups",
        "control_health_checks",
        "control_rollouts",
    } == set(migration.CONTROL_PLANE_TABLES)


def test_provisioning_runs_migration_tracks_expected_head():
    migration = _provisioning_runs_migration_module()

    assert migration.revision == "0006_provisioning_runs"
    assert migration.down_revision == "0005_control_plane_postgres"
    assert {"provisioning_runs", "one_time_secret_envelopes"} == set(migration.PROVISIONING_TABLES)


def test_governance_migration_extends_provisioning_runs():
    migration = _governance_migration_module()

    assert len(migration.revision) <= 32
    assert migration.down_revision == "0006_provisioning_runs"
    assert {
        "platform_organizations",
        "platform_memberships",
        "platform_consent_records",
        "platform_retention_policies",
        "platform_data_access_events",
        "platform_processor_register",
        "platform_provider_register",
        "platform_credential_metadata",
        "retention_runs",
    } == set(migration.GOVERNANCE_TABLES)


def test_rls_hardening_migration_structure():
    migration = _rls_hardening_module()

    assert migration.revision == "0008_rls_hardening"
    assert migration.down_revision == "0007_governance_privacy"
    assert "intake_records" in migration.TENANT_TABLES
    assert "platform_audit_events" in migration.ACCOUNT_TABLES


def test_rls_admin_role_migration_structure():
    migration = _rls_admin_role_module()

    assert migration.revision == "0009_rls_admin_role"
    assert migration.down_revision == "0008_rls_hardening"
    # The bypass no longer reads a session GUC; it is bound to the role identity.
    assert "app.onebrain_admin" not in migration._ROLE_ADMIN_FN
    assert "pg_has_role" in migration._ROLE_ADMIN_FN


def test_append_only_audit_migration_structure():
    migration = _append_only_audit_module()

    assert migration.revision == "0010_append_only_audit"
    assert migration.down_revision == "0009_rls_admin_role"
    # The audit table is made immutable to the app: UPDATE/DELETE/TRUNCATE raise.
    assert "append-only" in migration._FORBID_FN
    assert "restrict_violation" in migration._FORBID_FN


def _assistant_workday_contract_module():
    return _load_migration_module(
        "0011_assistant_workday_contract.py", "assistant_workday_contract_migration"
    )


def _auth_sessions_module():
    return _load_migration_module("0012_auth_sessions.py", "auth_sessions_migration")


def _legal_holds_module():
    return _load_migration_module("0013_legal_holds.py", "legal_holds_migration")


def test_auth_sessions_migration_structure():
    migration = _auth_sessions_module()

    assert migration.revision == "0012_auth_sessions"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0011_assistant_workday_contract"
    # The revocation table and the fast active/user lookup indexes must be created.
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0012_auth_sessions.py"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS auth_sessions" in src
    assert "revoked_at" in src
    assert "idx_auth_sessions_user" in src


def _tombstones_module():
    return _load_migration_module("0014_tombstones.py", "tombstones_migration")


def test_legal_holds_migration_structure():
    migration = _legal_holds_module()

    assert migration.revision == "0013_legal_holds"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0012_auth_sessions"
    # The hold table plus forced RLS mirroring the other account-scoped tables.
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0013_legal_holds.py"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS platform_legal_holds" in src
    assert "FORCE ROW LEVEL SECURITY" in src
    assert "onebrain_platform_legal_holds_scope" in src


def test_tombstones_migration_structure():
    migration = _tombstones_module()

    assert migration.revision == "0014_tombstones"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0013_legal_holds"
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0014_tombstones.py"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS platform_tombstones" in src
    assert "CREATE TABLE IF NOT EXISTS platform_tombstone_acks" in src
    assert "BIGSERIAL" in src
    assert "FORCE ROW LEVEL SECURITY" in src


def _fleet_telemetry_module():
    return _load_migration_module("0015_fleet_telemetry.py", "fleet_telemetry_migration")


def test_fleet_telemetry_migration_structure():
    migration = _fleet_telemetry_module()

    assert migration.revision == "0015_fleet_telemetry"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0014_tombstones"
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0015_fleet_telemetry.py"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS fleet_keys" in src
    assert "CREATE TABLE IF NOT EXISTS fleet_heartbeats" in src
    assert "CREATE TABLE IF NOT EXISTS fleet_alerts" in src


def _deployment_account_id_module():
    return _load_migration_module("0016_deployment_account_id.py", "deployment_account_id_migration")


def test_deployment_account_id_migration_structure():
    migration = _deployment_account_id_module()

    assert migration.revision == "0016_deployment_account_id"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0015_fleet_telemetry"
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0016_deployment_account_id.py"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS account_id" in src
    assert "control_deployments" in src


def _rollout_execution_module():
    return _load_migration_module("0017_rollout_execution.py", "rollout_execution_migration")


def test_rollout_execution_migration_structure():
    migration = _rollout_execution_module()

    assert migration.revision == "0017_rollout_execution"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0016_deployment_account_id"
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0017_rollout_execution.py"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS" in src
    assert "exec_status" in src
    assert "control_rollouts" in src


def _fleet_rollouts_module():
    return _load_migration_module("0018_fleet_rollouts.py", "fleet_rollouts_migration")


def test_fleet_rollouts_migration_structure():
    migration = _fleet_rollouts_module()

    assert migration.revision == "0018_fleet_rollouts"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0017_rollout_execution"
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0018_fleet_rollouts.py"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS control_fleet_rollouts" in src
    assert "failure_tolerance" in src


def _trust_primitives_module():
    return _load_migration_module("0019_trust_primitives.py", "trust_primitives_migration")


def _must_change_password_module():
    return _load_migration_module("0020_must_change_password.py", "must_change_password_migration")


def test_trust_primitives_migration_structure_and_chain():
    migration = _trust_primitives_module()

    # 0019 is no longer head (0020 supersedes it); it is the down_revision of head.
    assert migration.revision == "0019_trust_primitives"
    assert migration.revision != REQUIRED_ALEMBIC_REVISION
    assert migration.down_revision == "0018_fleet_rollouts"
    assert _must_change_password_module().down_revision == migration.revision
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0019_trust_primitives.py"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS" in src
    assert "update_policy" in src
    assert "rollback_kind" in src
    assert "ack_restore_required" in src
    assert "control_release_manifests" in src


def test_must_change_password_migration_is_head():
    migration = _must_change_password_module()

    assert migration.revision == REQUIRED_ALEMBIC_REVISION
    assert migration.revision == "0020_must_change_password"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0019_trust_primitives"
    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0020_must_change_password.py"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS" in src
    assert "users" in src
    assert "must_change_password" in src


# --- positional row mappers (C4) ----------------------------------------------
# Ground rule 3 (append-only positional columns) is arithmetic — grepping the
# migration source alone cannot catch an off-by-one, and there is no live
# Postgres harness, so a swapped index would first manifest on production
# Railway. These tests feed synthetic row tuples straight through the mappers.

def _bare_postgres_store():
    from app.controlplane.postgres import PostgresControlPlaneStore

    # Skip __init__ (DSN + schema validation) — the mappers are pure.
    return object.__new__(PostgresControlPlaneStore)


def test_postgres_row_mappers_positional():
    from datetime import datetime, timezone

    store = _bare_postgres_store()
    created = datetime(2026, 7, 12, 1, 2, 3, tzinfo=timezone.utc)
    dispatched = datetime(2026, 7, 12, 4, 5, 6, tzinfo=timezone.utc)
    completed = datetime(2026, 7, 12, 7, 8, 9, tzinfo=timezone.utc)

    deployment = store._deployment((
        "dep0", "cust1", "env2", "type3", "region4", "ring5", "status6",
        "ver7", "mig8", created, "acct10", "policy11",
    ))
    assert deployment.id == "dep0"
    assert deployment.customer_name == "cust1"
    assert deployment.environment == "env2"
    assert deployment.deployment_type == "type3"
    assert deployment.region == "region4"
    assert deployment.release_ring == "ring5"
    assert deployment.status == "status6"
    assert deployment.current_version == "ver7"
    assert deployment.current_migration == "mig8"
    assert deployment.created_at == created.isoformat()
    assert deployment.account_id == "acct10"
    assert deployment.update_policy == "policy11"

    release = store._release((
        "ver0", "sha1", {"m": "2"}, "from3", "to4", "notes5", "plan6",
        "status7", created, {"m": "img9"}, "kind10", "sig11", "keyid12",
    ))
    assert release.version == "ver0"
    assert release.git_sha == "sha1"
    assert release.modules == {"m": "2"}
    assert release.migration_from == "from3"
    assert release.migration_to == "to4"
    assert release.security_notes == "notes5"
    assert release.rollback_plan == "plan6"
    assert release.status == "status7"
    assert release.created_at == created.isoformat()
    assert release.images == {"m": "img9"}
    assert release.rollback_kind == "kind10"
    assert release.signature == "sig11"
    assert release.signing_key_id == "keyid12"

    rollout = store._rollout((
        "roll0", "dep1", "ver2", "status3", "by4", "notes5", created,
        "exec7", "provider8", "runid9", "runurl10", "fail11", {"k": "payload12"},
        dispatched, completed, "fleet15", True,
    ))
    assert rollout.id == "roll0"
    assert rollout.deployment_id == "dep1"
    assert rollout.target_version == "ver2"
    assert rollout.status == "status3"
    assert rollout.started_by == "by4"
    assert rollout.notes == "notes5"
    assert rollout.created_at == created.isoformat()
    assert rollout.exec_status == "exec7"
    assert rollout.external_provider == "provider8"
    assert rollout.external_run_id == "runid9"
    assert rollout.external_run_url == "runurl10"
    assert rollout.failure_reason == "fail11"
    assert rollout.request_payload == {"k": "payload12"}
    assert rollout.dispatched_at == dispatched.isoformat()
    assert rollout.completed_at == completed.isoformat()
    assert rollout.fleet_rollout_id == "fleet15"
    assert rollout.ack_restore_required is True


def test_rollout_cols_arity_matches_mapper():
    from app.controlplane.postgres import PostgresControlPlaneStore

    cols = PostgresControlPlaneStore._ROLLOUT_COLS.split(",")
    assert len(cols) == 17  # _rollout reads indexes 0..16
    assert cols[-1].strip() == "ack_restore_required"


def test_start_rollout_insert_persists_fleet_rollout_id():
    """Store parity (memory vs postgres): the memory store persists the whole
    RolloutRun dataclass, so a child's fleet_rollout_id linkage survives; the
    postgres INSERT must write the column too, or list_rollouts_for_fleet
    returns [] and ring reconciliation counts zero children. The mapper/arity
    tests above only cover the READ side — this drives the WRITE side through
    a stub cursor."""
    import re
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.controlplane.base import RolloutRun

    created = datetime(2026, 7, 12, 1, 2, 3, tzinfo=timezone.utc)

    class InsertCursor:
        def __init__(self):
            self.sql = ""
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.sql = sql
            self.params = params

        def fetchone(self):
            # Echo the INSERT params back through the 17-column RETURNING
            # shape so the read-back travels params -> row -> mapper.
            by_column = dict(zip(_insert_columns(self.sql), self.params))
            return (
                by_column["id"], by_column["deployment_id"],
                by_column["target_version"], by_column["status"],
                by_column["started_by"], by_column["notes"], created,
                "pending", "github_actions", "", "", "", {}, None, None,
                by_column["fleet_rollout_id"], by_column["ack_restore_required"],
            )

    class InsertConnection:
        def __init__(self, cursor):
            self._cursor = cursor
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return self._cursor

        def commit(self):
            self.committed = True

    def _insert_columns(sql: str) -> list[str]:
        column_list = re.search(r"INSERT INTO control_rollouts\s*\(([^)]*)\)", sql)
        assert column_list, sql
        return [column.strip() for column in column_list.group(1).split(",")]

    cursor = InsertCursor()
    connection = InsertConnection(cursor)
    store = _bare_postgres_store()
    store._conn = lambda: connection
    store.plan_update = lambda deployment_id, target_version, ack_restore_required=False: (
        SimpleNamespace(allowed=True, reason=""))

    returned = store.start_rollout(RolloutRun(
        id="roll_child", deployment_id="dep_a", target_version="2026.07.2",
        status="pending", started_by="fleet:fr_1", fleet_rollout_id="fr_1"))

    columns = _insert_columns(cursor.sql)
    assert "fleet_rollout_id" in columns
    assert len(columns) == len(cursor.params)  # write-side arity
    assert dict(zip(columns, cursor.params))["fleet_rollout_id"] == "fr_1"
    assert returned.fleet_rollout_id == "fr_1"  # readable back on the postgres path
    assert connection.committed


def test_assistant_workday_contract_migration_structure():
    migration = _assistant_workday_contract_module()

    assert migration.revision == "0011_assistant_workday_contract"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0010_append_only_audit"
    # The backfill targets only rows holding exactly the pre-Phase-4 full contract,
    # so deliberately narrowed keys/installations keep their narrow grants.
    assert "assistant_workday" not in migration._OLD_FULL_PURPOSES
    assert migration._NEW_PURPOSE == "assistant_workday"
    assert "array_agg(p ORDER BY p)" in migration._add_purpose_sql(
        "platform_app_installations", "allowed_purposes"
    )
    # FORCE RLS on platform_app_installations would silently skip the backfill under
    # the app role, so the migration must fail closed instead.
    assert "_onebrain_rls_admin" in migration._REQUIRE_RLS_ADMIN


def test_migration_embedding_dim_env(monkeypatch):
    migration = _baseline_migration_module()

    monkeypatch.setenv("ONEBRAIN_MIGRATION_EMBEDDING_DIM", "1024")

    assert migration._embedding_dim() == 1024


def test_baseline_migration_rejects_existing_chunks_dimension_mismatch(monkeypatch):
    migration = _baseline_migration_module()

    class FakeResult:
        def fetchone(self):
            return (1024,)

    class FakeBind:
        def execute(self, _sql):
            return FakeResult()

    class FakeOp:
        def get_bind(self):
            return FakeBind()

    monkeypatch.setattr(migration, "op", FakeOp())

    with pytest.raises(RuntimeError, match="embedding dimension 1024"):
        migration._assert_compatible_existing_chunks_table(256)


def test_baseline_migration_uses_legacy_adoption_sql():
    source = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0001_baseline_onebrain_schema.py"
    ).read_text()

    assert "CREATE TABLE IF NOT EXISTS chunks" in source
    assert "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tenant_id" in source
    assert "CREATE INDEX IF NOT EXISTS chunks_doc_id_idx" in source


def test_migration_guidance_mentions_alembic_command():
    assert "alembic upgrade head" in MIGRATION_GUIDANCE

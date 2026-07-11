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


def test_tombstones_migration_is_head():
    migration = _tombstones_module()

    assert migration.revision == REQUIRED_ALEMBIC_REVISION
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

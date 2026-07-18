from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.db.rls import RLS_REQUIRED_TABLES
from app.db.schema import REQUIRED_ALEMBIC_REVISION
from app.http_limits import request_body_limit
from app.drive.postgres import PostgresDriveStore


ROOT = Path(__file__).resolve().parents[1]
DRIVE_TABLES = {
    "drive_folders",
    "drive_files",
    "drive_file_revisions",
    "drive_upload_sessions",
}
ACCESS_GROUP_TABLES = {
    "platform_access_groups",
    "platform_access_group_memberships",
}


def _migration(filename: str, name: str):
    path = ROOT / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _captured_upgrade(monkeypatch, module) -> str:
    statements: list[str] = []
    monkeypatch.setenv("ONEBRAIN_POSTGRES_APP_ROLE", "onebrain_app")
    monkeypatch.setenv("ONEBRAIN_POSTGRES_WORKER_ROLE", "onebrain_worker")
    monkeypatch.setattr(module.op, "execute", lambda sql: statements.append(str(sql)))
    module.upgrade()
    return "\n".join(statements)


def test_drive_migration_is_additive_originals_stay_out_of_postgres_and_rls_is_forced(monkeypatch):
    module = _migration("0033_onebrain_drive.py", "drive_schema_migration")
    sql = _captured_upgrade(monkeypatch, module)

    assert module.revision == REQUIRED_ALEMBIC_REVISION
    assert module.down_revision == "0032_drive_foundations"
    assert set(module.DRIVE_TABLES) == DRIVE_TABLES
    assert "BYTEA" not in sql.upper()
    assert "storage_key TEXT NOT NULL UNIQUE" in sql
    assert "jobs_idempotency_idx" in sql
    assert "INSERT (idempotency_key)" in sql
    for table in DRIVE_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY onebrain_{table}_scope ON {table}" in sql
        # 0030 establishes default privileges, but an always-on module should
        # also grant its tables explicitly so upgrades fail neither when a
        # deployment changed migration owners nor during least-privilege audits.
        assert f"ON TABLE {table} TO \"onebrain_app\"" in sql


def test_foundation_migration_scopes_groups_and_purges_only_terminal_job_bytes(monkeypatch):
    module = _migration("0032_drive_foundations.py", "drive_foundation_migration")
    sql = _captured_upgrade(monkeypatch, module)

    assert module.down_revision == "0030_job_queue_rls_roles"
    for table in ACCESS_GROUP_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    sweep = (
        "DELETE FROM job_files jf USING jobs j "
        "WHERE jf.job_id = j.id AND j.status IN ('succeeded', 'failed')"
    )
    assert sweep in sql
    assert "onebrain_job_files_worker_delete" in sql
    assert "j.status IN ('succeeded', 'failed')" in sql


def test_schema_and_runtime_rls_registries_include_every_drive_table():
    assert REQUIRED_ALEMBIC_REVISION == "0033_onebrain_drive"
    assert DRIVE_TABLES <= set(RLS_REQUIRED_TABLES)
    assert ACCESS_GROUP_TABLES <= set(RLS_REQUIRED_TABLES)


def test_postgres_revision_replay_avoids_queries_in_an_aborted_unique_violation_transaction():
    source = (ROOT / "app" / "drive" / "postgres.py").read_text(encoding="utf-8")
    create_revision = source.split("def create_revision", 1)[1].split("def get_revision", 1)[0]

    assert "ON CONFLICT" in create_revision
    assert "same_revision_identity" in create_revision
    assert "except self._psycopg.errors.UniqueViolation" not in create_revision


def test_postgres_parent_validation_bounds_ancestor_and_subtree_depth():
    source = (ROOT / "app" / "drive" / "postgres.py").read_text(encoding="utf-8")
    validation = source.split("def _validate_parent", 1)[1]

    assert "WITH RECURSIVE ancestors" in validation
    assert "WITH RECURSIVE descendants" in validation
    assert "ancestor_depth + subtree_depth > MAX_FOLDER_DEPTH" in validation


def test_postgres_upload_lookup_sets_the_exact_account_scope_required_by_rls():
    scopes: list[dict] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @staticmethod
        def cursor():
            return Cursor()

    store = object.__new__(PostgresDriveStore)

    @contextmanager
    def connection(**scope):
        scopes.append(scope)
        yield Connection()

    store._conn = connection

    assert store.get_upload("upload_aaaaaaaa", tenant_id="tenant_account") is None
    assert scopes == [{"tenant_id": "tenant_account", "account_id": "tenant_account"}]


def test_drive_router_is_always_mounted_and_has_no_feature_flag():
    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    config_source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")

    assert "app.include_router(drive.router)" in main_source
    before, _separator, after = main_source.partition("app.include_router(drive.router)")
    # The adjacent Core routers are unconditional; Drive must not move under the
    # Mission Control/operator conditional later in create_app().
    assert "if settings.is_operator_surface" not in before[-300:]
    assert "app.include_router(jobs.router)" in after[:300]
    assert "drive_enabled" not in config_source


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("PUT", "/api/drive/uploads/upload_aaaaaaaa/content", 50_000),
        ("POST", "/api/drive/uploads/upload_aaaaaaaa/content", 1_000),
        ("PUT", "/api/drive/uploads/x/content", 1_000),
        ("PUT", "/api/drive/files/file_aaaaaaaa/content", 1_000),
        ("POST", "/api/chat", 1_000),
    ],
)
def test_large_body_allowance_is_exactly_the_raw_drive_upload_route(method, path, expected):
    assert request_body_limit(
        method,
        path,
        default_bytes=1_000,
        drive_file_bytes=50_000,
    ) == expected

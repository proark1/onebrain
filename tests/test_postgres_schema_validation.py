from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.db.schema import (
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


def test_validate_postgres_schema_requires_alembic_version_table():
    conn = FakeConnection(FakeCursor(missing_version_table=True))

    with pytest.raises(PostgresSchemaError, match="alembic upgrade head"):
        validate_postgres_schema(conn, ("users",))


def test_validate_postgres_schema_requires_current_baseline_revision():
    conn = FakeConnection(FakeCursor(version="old_revision"))

    with pytest.raises(PostgresSchemaError, match=REQUIRED_ALEMBIC_REVISION):
        validate_postgres_schema(conn, ("users",))


def test_validate_postgres_schema_reports_missing_tables():
    conn = FakeConnection(FakeCursor(missing_tables={"users"}))

    with pytest.raises(PostgresSchemaError, match="Missing tables: users"):
        validate_postgres_schema(conn, ("users",))


def test_baseline_migration_tracks_expected_tables_and_revision():
    migration = _baseline_migration_module()

    assert migration.revision == REQUIRED_ALEMBIC_REVISION
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


def test_migration_embedding_dim_env(monkeypatch):
    migration = _baseline_migration_module()

    monkeypatch.setenv("ONEBRAIN_MIGRATION_EMBEDDING_DIM", "1024")

    assert migration._embedding_dim() == 1024


def test_migration_guidance_mentions_alembic_command():
    assert "alembic upgrade head" in MIGRATION_GUIDANCE

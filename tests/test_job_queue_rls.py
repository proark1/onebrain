"""Regression coverage for the role-separated durable job queue."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.jobs.postgres as postgres_jobs
from app.db.rls import (
    PostgresRoleError,
    RLS_REQUIRED_TABLES,
    validate_job_role_configuration,
    validate_restricted_runtime_role,
)
from app.jobs.postgres import JobWorkerAccessError, PostgresJobStore
from app.jobs.base import JobEnqueueSpec


_AT = datetime(2026, 7, 17, tzinfo=timezone.utc)
_JOB_ROW = (
    "job_1", "document_ingest", "queued", "tenant_a", "account_a", "space_a", "user_a",
    {}, None, "", 0, 3, _AT, "", None, "", None, _AT, _AT, None,
)


class _Cursor:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor):
        self.cursor_obj = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


class _Psycopg:
    def __init__(self, connections):
        self.connections = connections
        self.connected: list[str] = []

    def connect(self, dsn):
        self.connected.append(dsn)
        return self.connections[dsn]


def _store(*, app_cursor=None, worker_cursor=None, operator_cursor=None) -> tuple[PostgresJobStore, _Psycopg]:
    store = object.__new__(PostgresJobStore)
    driver = _Psycopg(
        {
            "postgresql://app": _Connection(app_cursor or _Cursor()),
            "postgresql://worker": _Connection(worker_cursor or _Cursor()),
            "postgresql://operator": _Connection(operator_cursor or _Cursor()),
        }
    )
    store._psycopg = driver
    store._dsn = "postgresql://app"
    store._worker_dsn = "postgresql://worker"
    store._operator_dsn = "postgresql://operator"
    return store, driver


def _migration_module():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0030_job_queue_rls_roles.py"
    spec = importlib.util.spec_from_file_location("job_queue_rls_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_job_queue_migration_is_head_successor_with_role_scoped_policies():
    migration = _migration_module()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "0030_job_queue_rls_roles"
    assert migration.down_revision == "0029_auth_rate_limits"
    assert set(migration.JOB_TABLES) == {"jobs", "job_files"}
    assert migration.APP_ROLE_ENV == "ONEBRAIN_POSTGRES_APP_ROLE"
    assert migration.WORKER_ROLE_ENV == "ONEBRAIN_POSTGRES_WORKER_ROLE"
    assert "FOR SELECT TO {app_ident}" in source
    assert "FOR INSERT TO {app_ident}" in source
    assert "FOR SELECT TO {worker_ident}" in source
    assert "FOR UPDATE TO {worker_ident}" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "NOCREATEDB, NOCREATEROLE, NOINHERIT, NOBYPASSRLS, and NOREPLICATION" in source
    assert "must not be able to assume a superuser or BYPASSRLS role" in source
    assert "must be outside the owner/operator role hierarchy" in source
    assert "REVOKE ALL PRIVILEGES ON TABLE jobs, job_files FROM PUBLIC" in source
    assert "GRANT {_APP_DML_PRIVILEGES} ON ALL TABLES IN SCHEMA public" in source
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public" in source
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in source
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {worker_ident}" in source
    assert "GRANT SELECT ({_APP_JOB_SELECT_COLUMNS})" in source
    assert "GRANT INSERT ({_APP_JOB_FILE_INSERT_COLUMNS})" in source
    assert "payload" not in migration._APP_JOB_SELECT_COLUMNS
    assert "lease_token" not in migration._APP_JOB_SELECT_COLUMNS


def test_job_queue_migration_rejects_unsafe_or_missing_role_names(monkeypatch):
    migration = _migration_module()

    monkeypatch.delenv(migration.APP_ROLE_ENV, raising=False)
    with pytest.raises(RuntimeError, match=migration.APP_ROLE_ENV):
        migration._configured_role(migration.APP_ROLE_ENV)

    monkeypatch.setenv(migration.WORKER_ROLE_ENV, 'worker"; DROP ROLE x; --')
    with pytest.raises(RuntimeError, match=migration.WORKER_ROLE_ENV):
        migration._configured_role(migration.WORKER_ROLE_ENV)


def test_box_postgres_init_creates_distinct_restricted_queue_logins():
    source = (
        Path(__file__).resolve().parents[1] / "deploy" / "box" / "postgres-init.sh"
    ).read_text(encoding="utf-8")

    for env_name in (
        "POSTGRES_APP_ROLE",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_WORKER_ROLE",
        "POSTGRES_WORKER_PASSWORD",
        "POSTGRES_ASSISTANT_ROLE",
        "POSTGRES_ASSISTANT_PASSWORD",
        "POSTGRES_COMMUNICATION_ROLE",
        "POSTGRES_COMMUNICATION_PASSWORD",
    ):
        assert env_name in source
    assert "all runtime roles must differ" in source
    assert "runtime roles must not use POSTGRES_USER" in source
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION" in source
    assert "SET password_encryption = 'scram-sha-256'" in source
    assert "GRANT CONNECT ON DATABASE" in source
    assert "GRANT USAGE ON SCHEMA public" in source
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in source
    assert "REVOKE ALL PRIVILEGES ON DATABASE" in source
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE" in source
    assert "\\getenv app_password POSTGRES_APP_PASSWORD" in source
    assert "\\getenv worker_password POSTGRES_WORKER_PASSWORD" in source
    assert "\\getenv assistant_password POSTGRES_ASSISTANT_PASSWORD" in source
    assert "\\getenv communication_password POSTGRES_COMMUNICATION_PASSWORD" in source


def test_jobs_and_job_files_are_required_for_runtime_rls_validation():
    assert "jobs" in RLS_REQUIRED_TABLES
    assert "job_files" in RLS_REQUIRED_TABLES


def test_request_job_lookup_sets_scope_and_never_selects_payload(monkeypatch):
    app_cursor = _Cursor(row=_JOB_ROW)
    store, driver = _store(app_cursor=app_cursor)
    scopes = []
    monkeypatch.setattr(
        postgres_jobs,
        "set_rls_scope",
        lambda conn, **scope: scopes.append((conn, scope)),
    )

    job = store.get("job_1", tenant_id="tenant_a", account_id="account_a", space_id="space_a")

    assert job and job.payload == {}
    assert driver.connected == ["postgresql://app"]
    assert scopes == [
        (driver.connections["postgresql://app"], {
            "tenant_id": "tenant_a", "account_id": "account_a", "space_id": "space_a",
        })
    ]
    sql = app_cursor.executed[-1][0]
    assert "'{}'::jsonb AS payload" in sql
    assert "''::text AS lease_token" in sql
    assert "requested_by, payload, result" not in sql


def test_postgres_batch_enqueue_is_constant_for_new_and_existing_100_rows():
    resolved_rows = []
    for index in range(100):
        job_row = list(_JOB_ROW)
        job_row[0] = f"job_batch_{index:03d}"
        job_row[1] = "drive_file_ingest"
        job_row[11] = 0
        job_row[12] = None
        resolved_rows.append((index, *job_row))

    class BatchCursor(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "SELECT i.ordinal" in sql:
                self.rows = resolved_rows
            else:
                self.rows = []

    app_cursor = BatchCursor()
    store, driver = _store(app_cursor=app_cursor)
    specs = tuple(
        JobEnqueueSpec(
            job_id=f"job_batch_{index:03d}",
            type="drive_file_ingest",
            tenant_id="tenant_a",
            account_id="account_a",
            space_id="space_a",
            requested_by="user_a",
            payload={"file_id": f"file_{index:03d}"},
            idempotency_key=f"drive-batch:{index:03d}",
        )
        for index in range(100)
    )

    first = store.enqueue_many(specs)

    assert len(first) == 100
    assert driver.connected == ["postgresql://app"]
    assert len(app_cursor.executed) == 3
    assert "set_config" in app_cursor.executed[0][0]
    assert "jsonb_to_recordset" in app_cursor.executed[1][0]
    assert "INSERT INTO jobs" in app_cursor.executed[1][0]
    assert "jsonb_to_recordset" in app_cursor.executed[2][0]
    assert "JOIN jobs" in app_cursor.executed[2][0]
    assert len(json.loads(app_cursor.executed[1][1][0])) == 100

    second = store.enqueue_many(specs)

    assert [job.id for job in second] == [job.id for job in first]
    assert driver.connected == ["postgresql://app", "postgresql://app"]
    assert len(app_cursor.executed) == 6
    assert driver.connections["postgresql://app"].commits == 2


def test_postgres_batch_enqueue_rejects_mixed_rls_scopes_before_connecting():
    store, driver = _store()

    with pytest.raises(ValueError, match="one tenant/account/space scope"):
        store.enqueue_many((
            JobEnqueueSpec(
                type="drive_file_ingest",
                tenant_id="tenant_a",
                account_id="account_a",
                space_id="space_a",
                idempotency_key="drive-batch:a",
            ),
            JobEnqueueSpec(
                type="drive_file_ingest",
                tenant_id="tenant_a",
                account_id="account_a",
                space_id="space_b",
                idempotency_key="drive-batch:b",
            ),
        ))

    assert driver.connected == []


def test_file_reads_and_claims_use_only_the_worker_dsn():
    worker_cursor = _Cursor(
        row=("file_1", "job_1", "example.txt", "text/plain", 3, b"abc", _AT),
        rows=[],
    )
    store, driver = _store(worker_cursor=worker_cursor)

    uploaded = store.get_file("job_1")
    assert uploaded and uploaded.data == b"abc"
    assert driver.connected == ["postgresql://worker"]

    store.claim("worker_1")
    assert driver.connected == ["postgresql://worker", "postgresql://worker"]
    assert all("jobs" in sql for sql, _params in worker_cursor.executed[-2:])
    assert "RETURNING j.id" in worker_cursor.executed[-2][0]


def test_exhausted_final_lease_deletes_bytes_after_terminalizing_parent():
    class ExhaustedCursor(_Cursor):
        def __init__(self):
            super().__init__()
            self.fetchall_calls = 0

        def fetchall(self):
            self.fetchall_calls += 1
            return [("job_expired",)] if self.fetchall_calls == 1 else []

    worker_cursor = ExhaustedCursor()
    store, driver = _store(worker_cursor=worker_cursor)

    assert store.claim("worker_1") == []

    assert len(worker_cursor.executed) == 3
    assert "UPDATE jobs" in worker_cursor.executed[0][0]
    assert "RETURNING j.id" in worker_cursor.executed[0][0]
    assert worker_cursor.executed[1] == (
        "DELETE FROM job_files WHERE job_id = ANY(%s)",
        (["job_expired"],),
    )
    assert "WITH claimed AS" in worker_cursor.executed[2][0]
    assert driver.connections["postgresql://worker"].commits == 1


def test_terminal_transition_deletes_job_bytes_in_the_same_worker_transaction():
    terminal_row = list(_JOB_ROW)
    terminal_row[2] = "succeeded"
    worker_cursor = _Cursor(row=tuple(terminal_row))
    store, driver = _store(worker_cursor=worker_cursor)

    completed = store.mark_succeeded("job_1", {"ok": True}, lease_token="lease_1")

    assert completed.status == "succeeded"
    assert len(worker_cursor.executed) == 2
    assert "UPDATE jobs" in worker_cursor.executed[0][0]
    assert "lease_token = %s" in worker_cursor.executed[0][0]
    assert worker_cursor.executed[1] == (
        "DELETE FROM job_files WHERE job_id = %s",
        ("job_1",),
    )
    assert driver.connections["postgresql://worker"].commits == 1


def test_privacy_scope_delete_counts_files_without_selecting_their_data(monkeypatch):
    class ScopeCursor(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "SELECT count(*) FROM deleted" in sql:
                self.row = (2,)
            elif "DELETE FROM jobs" in sql:
                self.rowcount = 3

    app_cursor = ScopeCursor()
    store, driver = _store(app_cursor=app_cursor)
    scopes = []
    monkeypatch.setattr(
        postgres_jobs,
        "set_rls_scope",
        lambda conn, **scope: scopes.append((conn, scope)),
    )

    deleted = store.delete_scope(
        "tenant_a", account_id="account_a", space_id="space_a",
    )

    assert (deleted.jobs, deleted.files) == (3, 2)
    assert scopes == [
        (driver.connections["postgresql://app"], {
            "tenant_id": "tenant_a", "account_id": "account_a", "space_id": "space_a",
        })
    ]
    assert "DELETE FROM job_files" in app_cursor.executed[0][0]
    assert "RETURNING 1" in app_cursor.executed[0][0]
    assert "data" not in app_cursor.executed[0][0]
    assert "DELETE FROM jobs" in app_cursor.executed[1][0]
    assert driver.connections["postgresql://app"].commits == 1


def test_worker_only_methods_fail_closed_without_worker_dsn():
    store, _driver = _store()
    store._worker_dsn = ""

    with pytest.raises(JobWorkerAccessError, match="WORKER_DATABASE_URL"):
        store.get_file("job_1")


def test_job_factory_passes_the_separate_worker_and_operator_dsns(monkeypatch):
    import app.jobs.factory as job_factory

    captured = {}

    class FakeStore:
        def __init__(self, dsn, *, worker_dsn, operator_dsn):
            captured.update(dsn=dsn, worker_dsn=worker_dsn, operator_dsn=operator_dsn)

    monkeypatch.setattr(postgres_jobs, "PostgresJobStore", FakeStore)
    settings = SimpleNamespace(
        vector_store="pgvector",
        pg_database_url="postgresql://app",
        pg_worker_database_url="postgresql://worker",
        pg_operator_database_url="postgresql://operator",
    )

    job_factory.build_job_store(settings)

    assert captured == {
        "dsn": "postgresql://app",
        "worker_dsn": "postgresql://worker",
        "operator_dsn": "postgresql://operator",
    }


def test_job_role_configuration_requires_a_distinct_worker_login_when_requested():
    settings = SimpleNamespace(
        postgres_app_role="onebrain_app",
        postgres_worker_role="onebrain_worker",
        database_url="postgresql://app",
        worker_database_url="",
    )
    validate_job_role_configuration(settings)

    with pytest.raises(PostgresRoleError, match="ONEBRAIN_WORKER_DATABASE_URL"):
        validate_job_role_configuration(settings, require_worker_dsn=True)

    settings.worker_database_url = "postgresql://app"
    with pytest.raises(PostgresRoleError, match="must not equal"):
        validate_job_role_configuration(settings, require_worker_dsn=True)


def test_live_role_verification_rejects_an_owner_or_bypass_login():
    class RoleCursor:
        def __init__(self, row):
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return self.row

    class RoleConnection:
        def __init__(self, row):
            self.row = row

        def cursor(self):
            return RoleCursor(self.row)

    validate_restricted_runtime_role(
        RoleConnection(("onebrain_worker", False, False, False, False, False, False, False, False)),
        "onebrain_worker",
        purpose="job worker",
    )

    with pytest.raises(PostgresRoleError, match="NOBYPASSRLS"):
        validate_restricted_runtime_role(
            RoleConnection(("onebrain_worker", False, True, False, False, False, False, False, False)),
            "onebrain_worker",
            purpose="job worker",
        )

    with pytest.raises(PostgresRoleError, match="NOBYPASSRLS"):
        validate_restricted_runtime_role(
            RoleConnection(("onebrain_worker", False, False, False, False, False, False, False, True)),
            "onebrain_worker",
            purpose="job worker",
        )

    with pytest.raises(PostgresRoleError, match="NOINHERIT"):
        validate_restricted_runtime_role(
            RoleConnection(("onebrain_worker", False, False, True, False, False, False, False, False)),
            "onebrain_worker",
            purpose="job worker",
        )

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from app.deploy import runtime
from app.deploy import start as deploy_start


@dataclass
class FakeSettings:
    vector_store: str = "memory"
    database_url: str = ""
    migration_database_url: str = ""
    rls_enforced: bool = False
    environment: str = "local"

    @property
    def is_production_like(self) -> bool:
        return self.environment in {"prod", "production", "staging"}


def test_run_migrations_skips_memory_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    runtime.run_migrations_if_needed(FakeSettings())

    assert calls == []


def test_run_migrations_runs_alembic_for_postgres(monkeypatch):
    calls = []

    def fake_run(command, check, env):
        calls.append((command, check, env["ONEBRAIN_DATABASE_URL"]))

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime, "enforce_rls_if_needed", lambda settings: None)

    runtime.run_migrations_if_needed(
        FakeSettings(vector_store="pgvector", database_url="postgresql://user:pass@host/db")
    )

    assert calls == [
        ([sys.executable, "-m", "alembic", "upgrade", "head"], True, "postgresql://user:pass@host/db")
    ]


def test_run_migrations_uses_migration_database_url_when_present(monkeypatch):
    calls = []

    def fake_run(command, check, env):
        calls.append((command, check, env["ONEBRAIN_DATABASE_URL"]))

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime, "enforce_rls_if_needed", lambda settings: None)

    runtime.run_migrations_if_needed(
        FakeSettings(
            vector_store="pgvector",
            database_url="postgresql://app:pass@host/db",
            migration_database_url="postgresql://owner:pass@host/db",
        )
    )

    assert calls == [
        ([sys.executable, "-m", "alembic", "upgrade", "head"], True, "postgresql://owner:pass@host/db")
    ]


def test_run_migrations_requires_database_url_for_postgres():
    with pytest.raises(RuntimeError, match="ONEBRAIN_DATABASE_URL"):
        runtime.run_migrations_if_needed(FakeSettings(vector_store="pgvector"))


def test_api_command_uses_railway_port(monkeypatch):
    monkeypatch.setenv("PORT", "4321")

    assert runtime.api_command() == [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "4321",
    ]


def test_worker_command_uses_worker_module():
    assert runtime.worker_command() == [sys.executable, "-m", "app.workers.run"]


def test_deployment_process_defaults_to_api(monkeypatch):
    monkeypatch.delenv("ONEBRAIN_PROCESS", raising=False)

    assert runtime.deployment_process() == "api"


def test_deployment_process_accepts_worker(monkeypatch):
    monkeypatch.setenv("ONEBRAIN_PROCESS", " worker ")

    assert runtime.deployment_process() == "worker"


def test_deployment_process_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("ONEBRAIN_PROCESS", "scheduler")

    with pytest.raises(RuntimeError, match="ONEBRAIN_PROCESS"):
        runtime.deployment_process()


def test_start_dispatches_api(monkeypatch):
    calls = []
    monkeypatch.setenv("ONEBRAIN_PROCESS", "api")
    monkeypatch.setattr(deploy_start.start_api, "main", lambda: calls.append("api"))
    monkeypatch.setattr(deploy_start.start_worker, "main", lambda: calls.append("worker"))

    deploy_start.main()

    assert calls == ["api"]


def test_start_dispatches_worker(monkeypatch):
    calls = []
    monkeypatch.setenv("ONEBRAIN_PROCESS", "worker")
    monkeypatch.setattr(deploy_start.start_api, "main", lambda: calls.append("api"))
    monkeypatch.setattr(deploy_start.start_worker, "main", lambda: calls.append("worker"))

    deploy_start.main()

    assert calls == ["worker"]


def test_wait_for_schema_skips_memory_mode(monkeypatch):
    monkeypatch.setattr(runtime, "_float_env", lambda *args, **kwargs: pytest.fail("should not read wait env"))

    runtime.wait_for_schema_if_needed(FakeSettings())


def test_rls_enforcement_requires_postgres_mode():
    with pytest.raises(RuntimeError, match="RLS_ENFORCED"):
        runtime.enforce_rls_if_needed(FakeSettings(rls_enforced=True))


def test_production_like_runtime_requires_postgres():
    with pytest.raises(RuntimeError, match="ONEBRAIN_VECTOR_STORE=pgvector"):
        runtime.validate_runtime_safety(FakeSettings(environment="production"))


def test_production_like_runtime_requires_rls():
    with pytest.raises(RuntimeError, match="ONEBRAIN_RLS_ENFORCED=true"):
        runtime.validate_runtime_safety(
            FakeSettings(
                environment="staging",
                vector_store="pgvector",
                database_url="postgresql://user:pass@host/db",
            )
        )


def test_production_like_runtime_accepts_postgres_with_rls():
    runtime.validate_runtime_safety(
        FakeSettings(
            environment="production",
            vector_store="pgvector",
            database_url="postgresql://user:pass@host/db",
            rls_enforced=True,
        )
    )

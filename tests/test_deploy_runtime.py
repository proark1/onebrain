from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from app.deploy import runtime


@dataclass
class FakeSettings:
    vector_store: str = "memory"
    database_url: str = ""
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

    def fake_run(command, check):
        calls.append((command, check))

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime, "enforce_rls_if_needed", lambda settings: None)

    runtime.run_migrations_if_needed(
        FakeSettings(vector_store="pgvector", database_url="postgresql://user:pass@host/db")
    )

    assert calls == [([sys.executable, "-m", "alembic", "upgrade", "head"], True)]


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

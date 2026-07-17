from __future__ import annotations

import sys
from dataclasses import dataclass
from types import SimpleNamespace

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
    embeddings_provider: str = "local"
    embedding_dim: int = 256
    login_rate_limit_secret: str = "s" * 32
    trusted_proxy_cidrs: str = ""
    trusted_proxy_hops: int = 0
    postgres_app_role: str = "onebrain_app"
    postgres_worker_role: str = "onebrain_worker"
    worker_database_url: str = ""

    @property
    def is_production_like(self) -> bool:
        return self.environment in {"prod", "production", "staging"}

    @property
    def pg_worker_database_url(self) -> str:
        return self.worker_database_url


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


def test_api_command_honors_port_override(monkeypatch):
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


def test_worker_schema_wait_validates_both_runtime_roles(monkeypatch):
    """A worker's normal app DSN must be restricted before queue startup.

    The later queue-role validation alone is insufficient: a compromised or
    miswired worker could otherwise use an owner DSN for its tenant data work.
    """
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    connections = []
    calls = []

    def connect(dsn):
        connections.append(dsn)
        return Connection()

    monkeypatch.setenv("ONEBRAIN_PROCESS", "worker")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(runtime, "validate_postgres_schema", lambda *_args: None)
    monkeypatch.setattr(runtime, "validate_rls_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "validate_restricted_runtime_role",
        lambda _conn, role, *, purpose: calls.append((role, purpose)),
    )
    monkeypatch.setattr(runtime, "validate_embedding_runtime_contract", lambda *_args: None)

    settings = FakeSettings(
        environment="production",
        vector_store="pgvector",
        database_url="postgresql://app:pass@host/db",
        worker_database_url="postgresql://worker:pass@host/db",
        rls_enforced=True,
    )
    runtime.wait_for_schema_if_needed(settings, timeout_seconds=0, poll_seconds=0)

    assert connections == [settings.database_url, settings.worker_database_url]
    assert calls == [
        (settings.postgres_app_role, "application"),
        (settings.postgres_worker_role, "job worker"),
    ]


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


def test_production_like_runtime_requires_explicit_job_role_split():
    with pytest.raises(RuntimeError, match="ONEBRAIN_POSTGRES_APP_ROLE"):
        runtime.validate_runtime_safety(
            FakeSettings(
                environment="production",
                vector_store="pgvector",
                database_url="postgresql://user:pass@host/db",
                rls_enforced=True,
                postgres_app_role="",
            )
        )


def test_production_api_rejects_worker_queue_dsn(monkeypatch):
    monkeypatch.setenv("ONEBRAIN_PROCESS", "api")

    with pytest.raises(RuntimeError, match="must not set ONEBRAIN_WORKER_DATABASE_URL"):
        runtime.validate_runtime_safety(
            FakeSettings(
                environment="production",
                vector_store="pgvector",
                database_url="postgresql://app:pass@host/db",
                rls_enforced=True,
                worker_database_url="postgresql://worker:pass@host/db",
            )
        )


def test_production_worker_requires_a_distinct_worker_queue_dsn(monkeypatch):
    monkeypatch.setenv("ONEBRAIN_PROCESS", "worker")

    with pytest.raises(RuntimeError, match="set ONEBRAIN_WORKER_DATABASE_URL"):
        runtime.validate_runtime_safety(
            FakeSettings(
                environment="production",
                vector_store="pgvector",
                database_url="postgresql://app:pass@host/db",
                rls_enforced=True,
            )
        )


def test_production_like_runtime_requires_distinct_login_limit_secret():
    with pytest.raises(RuntimeError, match="ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"):
        runtime.validate_runtime_safety(
            FakeSettings(
                environment="production",
                vector_store="pgvector",
                database_url="postgresql://user:pass@host/db",
                rls_enforced=True,
                login_rate_limit_secret="",
            )
        )


def test_embedding_contract_skips_local_or_non_production_settings(monkeypatch):
    monkeypatch.setattr(runtime, "build_embedder", lambda settings: pytest.fail("should not build"), raising=False)

    runtime.validate_embedding_runtime_contract(FakeSettings())


def test_embedding_contract_probes_provider_and_validates_pgvector(monkeypatch):
    calls: list[object] = []
    embedder = type("Embedder", (), {"dim": 768, "probe": lambda self: calls.append("probe")})()

    import app.embeddings.factory as embedding_factory
    import app.store.factory as store_factory

    monkeypatch.setattr(embedding_factory, "build_embedder", lambda settings: embedder)
    monkeypatch.setattr(store_factory, "build_store", lambda settings, dim: calls.append((settings, dim)))

    settings = FakeSettings(
        environment="production",
        vector_store="pgvector",
        database_url="postgresql://user:pass@host/db",
        rls_enforced=True,
        embeddings_provider="litellm",
        embedding_dim=768,
    )
    runtime.validate_embedding_runtime_contract(settings)

    assert calls == ["probe", (settings, 768)]

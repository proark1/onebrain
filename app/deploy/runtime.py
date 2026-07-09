"""Deployment runtime helpers for API and worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterable

from app.config import Settings, get_settings
from app.db.rls import validate_rls_enabled
from app.db.schema import REQUIRED_ALEMBIC_REVISION, validate_postgres_schema


DEFAULT_SCHEMA_WAIT_SECONDS = 60.0
DEFAULT_SCHEMA_WAIT_POLL_SECONDS = 2.0
DEFAULT_WORKER_REQUIRED_TABLES = ("chunks", "jobs", "job_files")
DEFAULT_DEPLOYMENT_PROCESS = "api"
SUPPORTED_DEPLOYMENT_PROCESSES = {"api", "worker"}


def is_postgres_mode(settings: Settings) -> bool:
    return settings.vector_store == "pgvector"


def deployment_process(raw: str | None = None) -> str:
    process = (raw if raw is not None else os.environ.get("ONEBRAIN_PROCESS", "")).strip().lower()
    process = process or DEFAULT_DEPLOYMENT_PROCESS
    if process not in SUPPORTED_DEPLOYMENT_PROCESSES:
        supported = ", ".join(sorted(SUPPORTED_DEPLOYMENT_PROCESSES))
        raise RuntimeError(f"ONEBRAIN_PROCESS must be one of: {supported}.")
    return process


def validate_runtime_safety(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.is_production_like:
        return
    if not is_postgres_mode(settings):
        raise RuntimeError(
            "Production-like OneBrain environments must set "
            "ONEBRAIN_VECTOR_STORE=pgvector."
        )
    _require_database_url(settings)
    if not settings.rls_enforced:
        raise RuntimeError(
            "Production-like OneBrain environments must set "
            "ONEBRAIN_RLS_ENFORCED=true."
        )


def run_migrations_if_needed(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    validate_runtime_safety(settings)
    if not is_postgres_mode(settings):
        print("Skipping Alembic migrations; ONEBRAIN_VECTOR_STORE is not pgvector.", flush=True)
        return

    _require_database_url(settings)
    migration_database_url = _migration_database_url(settings)
    print("Running Alembic migrations before API startup.", flush=True)
    migration_env = os.environ.copy()
    migration_env["ONEBRAIN_DATABASE_URL"] = migration_database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env=migration_env,
    )
    enforce_rls_if_needed(settings)


def wait_for_schema_if_needed(
    settings: Settings | None = None,
    required_tables: Iterable[str] = DEFAULT_WORKER_REQUIRED_TABLES,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> None:
    settings = settings or get_settings()
    if not is_postgres_mode(settings):
        print("Skipping Postgres schema wait; ONEBRAIN_VECTOR_STORE is not pgvector.", flush=True)
        return

    database_url = _require_database_url(settings)
    timeout_seconds = timeout_seconds if timeout_seconds is not None else _float_env(
        "ONEBRAIN_SCHEMA_WAIT_SECONDS", DEFAULT_SCHEMA_WAIT_SECONDS
    )
    poll_seconds = poll_seconds if poll_seconds is not None else _float_env(
        "ONEBRAIN_SCHEMA_WAIT_POLL_SECONDS", DEFAULT_SCHEMA_WAIT_POLL_SECONDS
    )

    import psycopg

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_error: Exception | None = None
    while True:
        try:
            with psycopg.connect(database_url) as conn:
                validate_postgres_schema(conn, required_tables)
                if settings.rls_enforced:
                    validate_rls_enabled(conn)
            print(
                f"Postgres schema is ready at Alembic revision {REQUIRED_ALEMBIC_REVISION}.",
                flush=True,
            )
            return
        except Exception as exc:  # database may still be booting or migrating
            last_error = exc

        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Postgres schema was not ready before worker startup timeout. "
                f"Required Alembic revision: {REQUIRED_ALEMBIC_REVISION}."
            ) from last_error

        print(f"Waiting for Postgres schema: {last_error}", flush=True)
        time.sleep(max(0.1, poll_seconds))


def enforce_rls_if_needed(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.rls_enforced:
        return
    if not is_postgres_mode(settings):
        raise RuntimeError("ONEBRAIN_RLS_ENFORCED=true requires ONEBRAIN_VECTOR_STORE=pgvector.")
    database_url = _require_database_url(settings)

    import psycopg

    with psycopg.connect(database_url) as conn:
        validate_rls_enabled(conn)
    print("Postgres RLS enforcement check passed.", flush=True)


def api_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("PORT", "8000"),
    ]


def worker_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.run"]


def exec_process(command: list[str]) -> None:
    print(f"Starting process: {' '.join(command)}", flush=True)
    os.execvp(command[0], command)


def _require_database_url(settings: Settings) -> str:
    database_url = settings.database_url.strip()
    if not database_url:
        raise RuntimeError("ONEBRAIN_DATABASE_URL must be set when ONEBRAIN_VECTOR_STORE=pgvector.")
    return database_url


def _migration_database_url(settings: Settings) -> str:
    return settings.migration_database_url.strip() or _require_database_url(settings)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number of seconds.") from exc

"""Deployment runtime helpers for API and worker processes."""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import time
from collections.abc import Iterable

from app.config import Settings, get_settings
from app.db.rls import (
    validate_job_role_configuration,
    validate_restricted_runtime_role,
    validate_rls_enabled,
)
from app.db.schema import REQUIRED_ALEMBIC_REVISION, validate_postgres_schema


DEFAULT_SCHEMA_WAIT_SECONDS = 60.0
DEFAULT_SCHEMA_WAIT_POLL_SECONDS = 2.0
DEFAULT_WORKER_REQUIRED_TABLES = (
    "chunks", "jobs", "job_files", "drive_files", "drive_file_revisions", "drive_upload_sessions",
    "drive_revision_malware_scans", "drive_malware_runtime_status",
    "drive_malware_activation_state",
    "drive_malware_settings",
)
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
    drive_mode = (settings.drive_policy_mode or "").strip().lower()
    if drive_mode not in {"disabled", "storage_only", "storage_and_indexing"}:
        raise RuntimeError(
            "ONEBRAIN_DRIVE_POLICY_MODE must be disabled, storage_only, or storage_and_indexing."
        )
    if settings.is_production_like and settings.drive_private_spaces_enabled:
        raise RuntimeError(
            "ONEBRAIN_DRIVE_PRIVATE_SPACES_ENABLED is not production-supported until "
            "private-owner transfer revocation ships."
        )
    if (
        settings.is_production_like
        and not getattr(settings, "operator_mode", False)
        and drive_mode == "storage_and_indexing"
        and settings.pii_phase != "dpia_signed"
    ):
        raise RuntimeError(
            "Drive AI indexing requires ONEBRAIN_PII_PHASE=dpia_signed in production. "
            "Use ONEBRAIN_DRIVE_POLICY_MODE=storage_only until the deployment DPIA is signed."
        )
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
    # Job queue policies distinguish a request-only application login from the
    # worker login that is allowed to claim work across tenants.  The API never
    # needs the worker password, but both identities must be named explicitly.
    validate_job_role_configuration(settings)
    process = deployment_process()
    if process == "api" and settings.worker_database_url.strip():
        raise RuntimeError(
            "API replicas must not set ONEBRAIN_WORKER_DATABASE_URL; "
            "only the worker service may receive the cross-tenant queue login."
        )
    if process == "worker":
        validate_job_role_configuration(settings, require_worker_dsn=True)
    if len(settings.login_rate_limit_secret) < 32:
        raise RuntimeError(
            "Production-like OneBrain environments must set a dedicated "
            "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET of at least 32 characters."
        )
    if settings.trusted_proxy_hops < 0:
        raise RuntimeError("ONEBRAIN_TRUSTED_PROXY_HOPS cannot be negative.")
    if settings.trusted_proxy_hops and not settings.trusted_proxy_cidrs.strip():
        raise RuntimeError(
            "ONEBRAIN_TRUSTED_PROXY_HOPS requires ONEBRAIN_TRUSTED_PROXY_CIDRS; "
            "forwarded client headers are otherwise ignored."
        )
    try:
        for cidr in filter(None, (value.strip() for value in settings.trusted_proxy_cidrs.split(","))):
            ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise RuntimeError("ONEBRAIN_TRUSTED_PROXY_CIDRS must contain valid CIDR ranges.") from exc


def validate_embedding_runtime_contract(settings: Settings | None = None) -> None:
    """Fail closed before serving with an incompatible live embedding stack.

    Local and development stacks intentionally remain keyless.  A
    production-like LiteLLM + pgvector deployment must prove that the provider
    honours the configured dimension and that the migrated column uses the same
    dimension.  Neither check may change persisted data or mutate configuration.
    """
    settings = settings or get_settings()
    if not (
        settings.is_production_like
        and settings.embeddings_provider == "litellm"
        and is_postgres_mode(settings)
    ):
        return

    from app.embeddings.factory import build_embedder
    from app.store.factory import build_store

    embedder = build_embedder(settings)
    probe = getattr(embedder, "probe", None)
    if not callable(probe):
        raise RuntimeError("Configured production embedding provider does not support provider preflight.")
    probe()
    # PgVectorStore validates the migrated column dimension without changing it.
    build_store(settings, dim=embedder.dim)


def run_migrations_if_needed(
    settings: Settings | None = None, *, require_malware_active: bool = True,
) -> None:
    settings = settings or get_settings()
    validate_runtime_safety(settings)
    if not is_postgres_mode(settings):
        print("Skipping Alembic migrations; ONEBRAIN_VECTOR_STORE is not pgvector.", flush=True)
        return

    database_url = _require_database_url(settings)
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
    # Revision 0033 is schema-only. The maintenance entry point deliberately
    # passes require_malware_active=False, runs bounded activation, and only
    # then starts services. Normal API startup keeps this guard enabled.
    if not require_malware_active:
        return
    import psycopg

    with psycopg.connect(database_url) as connection:
        validate_postgres_schema(
            connection,
            DEFAULT_WORKER_REQUIRED_TABLES,
            require_malware_active=True,
        )


def wait_for_schema_if_needed(
    settings: Settings | None = None,
    required_tables: Iterable[str] = DEFAULT_WORKER_REQUIRED_TABLES,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> None:
    settings = settings or get_settings()
    # Workers previously skipped the API startup safety guard entirely.  Run it
    # here too before the schema wait so a production worker cannot start with a
    # weak/non-RLS configuration.
    validate_runtime_safety(settings)
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
                validate_postgres_schema(conn, required_tables, require_malware_active=True)
                if settings.rls_enforced:
                    validate_rls_enabled(conn)
                # The worker uses this regular application connection for
                # tenant-scoped reads and writes before it switches to its
                # separate queue-only login.  Validate it here as well, so a
                # worker cannot accidentally receive an owner/BYPASSRLS DSN
                # and still pass the later worker-role check.
                if settings.is_production_like and settings.rls_enforced:
                    validate_restricted_runtime_role(
                        conn,
                        settings.postgres_app_role,
                        purpose="application",
                    )
        except Exception as exc:  # database may still be booting or migrating
            last_error = exc
        else:
            break

        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Postgres schema was not ready before worker startup timeout. "
                f"Required Alembic revision: {REQUIRED_ALEMBIC_REVISION}."
            ) from last_error

        print(f"Waiting for Postgres schema: {last_error}", flush=True)
        time.sleep(max(0.1, poll_seconds))

    # A provider/configuration failure is not a transient schema error. Surface
    # it immediately rather than retrying it until the schema timeout expires.
    if settings.is_production_like and settings.rls_enforced:
        validate_job_role_configuration(settings, require_worker_dsn=True)
        with psycopg.connect(settings.pg_worker_database_url) as worker_conn:
            validate_rls_enabled(worker_conn, ("jobs", "job_files"))
            validate_restricted_runtime_role(
                worker_conn,
                settings.postgres_worker_role,
                purpose="job worker",
            )
    validate_embedding_runtime_contract(settings)
    print(
        f"Postgres schema is ready at Alembic revision {REQUIRED_ALEMBIC_REVISION}.",
        flush=True,
    )


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
        if settings.is_production_like:
            validate_job_role_configuration(settings)
            validate_restricted_runtime_role(
                conn,
                settings.postgres_app_role,
                purpose="application",
            )
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

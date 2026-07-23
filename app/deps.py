"""Composition root — wires the swappable pieces into singletons.

Each component is chosen by config and built once. Because everything depends on
interfaces (Embedder / VectorStore / LLM), moving from the local prototype to a
production stack is a config change here, not a rewrite elsewhere.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings
from app.conversations.factory import build_conversation_store
from app.users.factory import build_user_store
from app.embeddings.factory import build_embedder
from app.ingest.pipeline import IngestPipeline
from app.intake.pipeline import IntakePipeline
from app.llm.factory import build_llm
from app.retrieval.service import RetrievalService
from app.store.factory import build_store

if TYPE_CHECKING:
    from app.drive.base import DriveMalwareWorkerStore, DriveStore


@lru_cache
def get_embedder():
    return build_embedder(get_settings())


@lru_cache
def get_store():
    return build_store(get_settings(), dim=get_embedder().dim)


@lru_cache
def get_llm():
    return build_llm(get_settings())


def _resolve_space_kind_and_owner(space_id: str) -> tuple[str, str]:
    """Resolve complete ingest labels or reject an unsafe/ambiguous scope.

    A private space must have exactly one owner, with the account owner used only
    as a fallback. Unknown spaces and unresolved ownership fail closed.
    """
    from app.platform.base import PRIVATE_SPACE_KINDS

    store = get_platform_store()
    space = store.get_space(space_id)
    if not space:
        raise ValueError("Unknown ingestion space; refusing unresolved access labels.")
    if space.kind not in PRIVATE_SPACE_KINDS:
        return space.kind, ""
    candidates = {
        m.user_id for m in store.list_memberships(space.account_id)
        if m.space_id == space.id and m.status == "active" and m.user_id
    }
    account = store.get_account(space.account_id)
    if not candidates and account and account.owner_user_id:
        candidates.add(account.owner_user_id)
    if len(candidates) != 1:
        raise ValueError("Private ingestion space ownership is unresolved or ambiguous.")
    return space.kind, next(iter(candidates))


@lru_cache
def get_pipeline() -> IngestPipeline:
    return IngestPipeline(get_embedder(), get_store(), space_resolver=_resolve_space_kind_and_owner)


@lru_cache
def get_retrieval_service() -> RetrievalService:
    settings = get_settings()
    return RetrievalService(
        get_embedder(),
        get_store(),
        get_llm(),
        top_k=settings.top_k,
        min_score=settings.retrieval_min_score,
    )


@lru_cache
def get_conversation_store():
    return build_conversation_store(get_settings())


@lru_cache
def get_user_store():
    return build_user_store(get_settings())


@lru_cache
def get_user_management_job_store():
    from app.user_management.factory import build_user_management_job_store

    return build_user_management_job_store(get_settings())


@lru_cache
def get_user_management_receipt_store():
    from app.user_management.factory import build_user_management_receipt_store

    return build_user_management_receipt_store(get_settings())


@lru_cache
def get_session_store():
    from app.sessions.factory import build_session_store

    return build_session_store(get_settings())


@lru_cache
def get_login_throttle():
    settings = get_settings()
    if settings.vector_store == "pgvector":
        from app.auth.login_limits import PostgresLoginThrottle

        return PostgresLoginThrottle(
            settings.pg_database_url,
            settings.login_rate_limit_secret,
            settings.login_max_attempts,
            settings.login_lockout_seconds,
        )

    from app.auth.throttle import LoginThrottle

    return LoginThrottle(settings.login_max_attempts, settings.login_lockout_seconds)


@lru_cache
def get_service_key_store():
    from app.servicekeys.factory import build_service_key_store

    return build_service_key_store(get_settings())


@lru_cache
def get_service_rate_limiter():
    settings = get_settings()
    if settings.vector_store == "pgvector":
        from app.auth.login_limits import PostgresRateLimiter

        return PostgresRateLimiter(
            settings.pg_database_url,
            settings.login_rate_limit_secret,
            settings.service_rate_limit,
            settings.service_rate_window_seconds,
            scope="service_key",
        )

    from app.auth.throttle import RateLimiter

    return RateLimiter(settings.service_rate_limit, settings.service_rate_window_seconds)


@lru_cache
def get_platform_store():
    from app.platform.factory import build_platform_store

    return build_platform_store(get_settings())


@lru_cache
def get_kpi_store():
    from app.kpis.factory import build_kpi_store

    return build_kpi_store(get_settings())


@lru_cache
def get_accounting_store():
    from app.accounting.factory import build_accounting_store

    return build_accounting_store(get_settings())


@lru_cache
def get_drive_store() -> DriveStore:
    from app.drive.factory import build_drive_store

    return build_drive_store(get_settings(), get_store(), dim=get_embedder().dim)


@lru_cache
def get_drive_worker_store() -> DriveMalwareWorkerStore:
    """Build Drive persistence with the separate worker capability attached.

    The API-facing singleton above never receives the worker DSN. This keeps
    fenced malware evidence transitions unavailable to request handlers even
    when API and worker modules are packaged in the same Core image.
    """
    from app.drive.factory import build_drive_worker_store

    settings = get_settings()
    return build_drive_worker_store(
        settings,
        get_store(),
        dim=get_embedder().dim,
        worker_dsn=settings.pg_worker_database_url,
    )


@lru_cache
def get_drive_blob_store():
    from app.drive.factory import build_drive_blob_store

    return build_drive_blob_store(get_settings())


@lru_cache
def get_drive_malware_scanner():
    from app.drive.malware.factory import build_drive_malware_scanner

    return build_drive_malware_scanner(get_settings())


@lru_cache
def get_drive_malware_scanning_service():
    from app.drive.scanning import DriveMalwareScanningService

    return DriveMalwareScanningService(
        store=get_drive_worker_store(),
        blobs=get_drive_blob_store(),
        scanner=get_drive_malware_scanner(),
        job_store=get_job_store(),
        platform_store=get_platform_store(),
        settings=get_settings(),
        worker_id=get_settings().drive_malware_worker_id,
    )


@lru_cache
def get_drive_service():
    from app.drive.service import DriveService

    return DriveService(
        store=get_drive_store(),
        blobs=get_drive_blob_store(),
        platform_store=get_platform_store(),
        job_store=get_job_store(),
        settings=get_settings(),
    )


@lru_cache
def get_ai_employee_store():
    from app.ai_employees.factory import build_ai_employee_store

    return build_ai_employee_store(get_settings())


@lru_cache
def get_ai_employee_backend_registry():
    from app.ai_employees.backends.factory import build_ai_employee_backend_registry

    return build_ai_employee_backend_registry(get_settings())


@lru_cache
def get_ai_employee_runtime():
    from app.ai_employees.runtime import AiEmployeeRuntime

    settings = get_settings()
    return AiEmployeeRuntime(
        store=get_ai_employee_store(),
        retrieval_service=get_retrieval_service(),
        backend_registry=get_ai_employee_backend_registry(),
        max_output_tokens=settings.ai_employees_max_output_tokens,
        run_lease_seconds=settings.ai_employees_run_lease_seconds,
        run_heartbeat_seconds=settings.ai_employees_run_heartbeat_seconds,
        provider_timeout_seconds=settings.ai_employees_provider_timeout_seconds,
    )


@lru_cache
def get_ai_employee_mission_service():
    from app.ai_employees.missions import AiMissionService, ModelMissionAgentExecutor

    settings = get_settings()
    return AiMissionService(
        store=get_ai_employee_store(),
        retrieval_service=get_retrieval_service(),
        executor=ModelMissionAgentExecutor(
            get_ai_employee_backend_registry(),
            max_output_tokens=settings.ai_employees_max_output_tokens,
        ),
    )


@lru_cache
def get_ai_employee_action_executor_registry():
    from app.ai_employees.actions import ActionExecutorRegistry

    return ActionExecutorRegistry([get_ai_employee_google_calendar_connector()])


@lru_cache
def get_ai_employee_google_calendar_connector():
    from app.ai_employees.connectors.factory import build_google_calendar_connector

    return build_google_calendar_connector(get_settings(), get_ai_employee_store())


@lru_cache
def get_ai_employee_action_service():
    from app.ai_employees.actions import AiEmployeeActionService

    return AiEmployeeActionService(
        store=get_ai_employee_store(),
        intake_store=get_intake_store(),
        session_store=get_session_store(),
        executor_registry=get_ai_employee_action_executor_registry(),
    )


@lru_cache
def get_control_plane_store():
    from app.controlplane.factory import build_control_plane_store

    return build_control_plane_store(get_settings())


@lru_cache
def get_fleet_store():
    from app.fleet.factory import build_fleet_store

    return build_fleet_store(get_settings())


@lru_cache
def get_fleet_heartbeat_rate_limiter():
    settings = get_settings()
    if settings.vector_store == "pgvector":
        from app.auth.login_limits import PostgresRateLimiter

        return PostgresRateLimiter(
            settings.pg_database_url,
            settings.login_rate_limit_secret,
            settings.fleet_heartbeat_rate_limit,
            settings.fleet_heartbeat_rate_window_seconds,
            scope="fleet_heartbeat",
        )

    from app.auth.throttle import RateLimiter

    return RateLimiter(settings.fleet_heartbeat_rate_limit, settings.fleet_heartbeat_rate_window_seconds)


@lru_cache
def get_fleet_bootstrap_rate_limiter():
    # G1-5: a DEDICATED, aggressively-low limiter for the /api/fleet/bootstrap secret
    # exchange — never the heartbeat budget (one fetch exfiltrates the whole bundle).
    settings = get_settings()
    if settings.vector_store == "pgvector":
        from app.auth.login_limits import PostgresRateLimiter

        return PostgresRateLimiter(
            settings.pg_database_url,
            settings.login_rate_limit_secret,
            settings.fleet_bootstrap_rate_limit,
            settings.fleet_bootstrap_rate_window_seconds,
            scope="fleet_bootstrap",
        )

    from app.auth.throttle import RateLimiter

    return RateLimiter(settings.fleet_bootstrap_rate_limit, settings.fleet_bootstrap_rate_window_seconds)


@lru_cache
def get_intake_store():
    from app.intake.factory import build_intake_store

    return build_intake_store(get_settings())


@lru_cache
def get_intake_pipeline() -> IntakePipeline:
    return IntakePipeline(get_intake_store(), get_settings())


@lru_cache
def get_job_store():
    from app.jobs.factory import build_job_store

    return build_job_store(get_settings())


@lru_cache
def get_provisioning_run_store():
    from app.provisioning.factory import build_provisioning_run_store

    return build_provisioning_run_store(get_settings())

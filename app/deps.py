"""Composition root — wires the swappable pieces into singletons.

Each component is chosen by config and built once. Because everything depends on
interfaces (Embedder / VectorStore / LLM), moving from the local prototype to a
production stack is a config change here, not a rewrite elsewhere.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.conversations.factory import build_conversation_store
from app.users.factory import build_user_store
from app.embeddings.factory import build_embedder
from app.ingest.pipeline import IngestPipeline
from app.intake.pipeline import IntakePipeline
from app.llm.factory import build_llm
from app.retrieval.service import RetrievalService
from app.store.factory import build_store


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
    """space_id -> (space_kind, owner_user_id) for ingest labelling.

    Owner of a private space is the employee bound to it by membership, falling
    back to the account owner. A non-private space returns no owner. Resolution
    failures return ("", "") — the chunk is then stamped with no space_kind and
    is treated as non-private, so this can never accidentally widen access.
    """
    from app.platform.base import PRIVATE_SPACE_KINDS

    store = get_platform_store()
    space = store.get_space(space_id)
    if not space:
        return "", ""
    if space.kind not in PRIVATE_SPACE_KINDS:
        return space.kind, ""
    for m in store.list_memberships(space.account_id):
        if m.space_id == space.id and m.status == "active" and m.user_id:
            return space.kind, m.user_id
    account = store.get_account(space.account_id)
    return space.kind, (account.owner_user_id if account else "")


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
def get_session_store():
    from app.sessions.factory import build_session_store

    return build_session_store(get_settings())


@lru_cache
def get_login_throttle():
    from app.auth.throttle import LoginThrottle

    settings = get_settings()
    return LoginThrottle(settings.login_max_attempts, settings.login_lockout_seconds)


@lru_cache
def get_service_key_store():
    from app.servicekeys.factory import build_service_key_store

    return build_service_key_store(get_settings())


@lru_cache
def get_service_rate_limiter():
    from app.auth.throttle import RateLimiter

    settings = get_settings()
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
def get_control_plane_store():
    from app.controlplane.factory import build_control_plane_store

    return build_control_plane_store(get_settings())


@lru_cache
def get_fleet_store():
    from app.fleet.factory import build_fleet_store

    return build_fleet_store(get_settings())


@lru_cache
def get_fleet_heartbeat_rate_limiter():
    from app.auth.throttle import RateLimiter

    settings = get_settings()
    return RateLimiter(settings.fleet_heartbeat_rate_limit, settings.fleet_heartbeat_rate_window_seconds)


@lru_cache
def get_fleet_bootstrap_rate_limiter():
    # G1-5: a DEDICATED, aggressively-low limiter for the /api/fleet/bootstrap secret
    # exchange — never the heartbeat budget (one fetch exfiltrates the whole bundle).
    from app.auth.throttle import RateLimiter

    settings = get_settings()
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

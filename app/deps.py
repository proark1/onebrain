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


@lru_cache
def get_pipeline() -> IngestPipeline:
    return IngestPipeline(get_embedder(), get_store())


@lru_cache
def get_retrieval_service() -> RetrievalService:
    return RetrievalService(get_embedder(), get_store(), get_llm(), get_settings().top_k)


@lru_cache
def get_conversation_store():
    return build_conversation_store(get_settings())


@lru_cache
def get_user_store():
    return build_user_store(get_settings())


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
def get_control_plane_store():
    from app.controlplane.factory import build_control_plane_store

    return build_control_plane_store(get_settings())


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

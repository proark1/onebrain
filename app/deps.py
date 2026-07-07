"""Composition root — wires the swappable pieces into singletons.

Each component is chosen by config and built once. Because everything depends on
interfaces (Embedder / VectorStore / LLM), moving from the local prototype to a
production stack is a config change here, not a rewrite elsewhere.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.embeddings.factory import build_embedder
from app.ingest.pipeline import IngestPipeline
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

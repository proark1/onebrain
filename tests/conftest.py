"""Shared fixtures: a fully-wired service on synthetic seed data, no API keys."""

from __future__ import annotations

import pytest

from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.llm.local import LocalLLM
from app.retrieval.service import RetrievalService
from app.seed import seed_if_empty
from app.store.memory import MemoryStore


@pytest.fixture
def store():
    s = MemoryStore(persist_path=None)
    seed_if_empty(IngestPipeline(LocalEmbedder(), s), s)
    return s


@pytest.fixture
def service(store):
    return RetrievalService(LocalEmbedder(), store, LocalLLM(), top_k=8)


def principal_for(role_id: str, location: str = "munich") -> Principal:
    role = ROLES[role_id]
    if role.scope == "chain":
        locations = None
    elif role.scope == "location":
        locations = frozenset({location})
    else:
        locations = frozenset()
    return Principal(
        user_id=f"{role_id}@{location}",
        role_id=role_id,
        role_label=role.label,
        clearance=role.clearance,
        locations=locations,
        categories=role.categories,
        location_label=location,
    )

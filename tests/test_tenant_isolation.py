"""The load-bearing multi-tenant test: tenant A can NEVER see tenant B's data.

Seeds NFT Gym plus an adversarial Company-B chunk that is deliberately
mislabeled (public / global / general) and worded to match an NFT Gym query —
so ONLY the tenant boundary can keep it out. Then proves it stays out for every
NFT Gym role, including admin (whose categories filter is disabled).
"""

from __future__ import annotations

import pytest

from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.llm.local import LocalLLM
from app.retrieval.service import RetrievalService
from app.security.policy import AccessFilter, Classification
from app.seed import seed_if_empty
from app.store.memory import MemoryStore
from tests.conftest import principal_for

B_TITLE = "Company B opening hours"


@pytest.fixture
def two_tenant_store():
    emb = LocalEmbedder()
    store = MemoryStore(persist_path=None)
    pipe = IngestPipeline(emb, store)
    seed_if_empty(pipe, store, tenant="nft_gym")
    # Adversarial: same topic as an NFT Gym public doc, mislabeled maximally open,
    # different tenant. Nothing but tenant_id should keep this away from NFT Gym.
    pipe.ingest_text(
        title=B_TITLE,
        text="Company B secret. Our opening hours and class schedule and membership pricing.",
        classification="public", location="global", category="general",
        uploaded_by="companyB", tenant="companyB",
    )
    return store, emb


def test_nft_gym_never_sees_company_b_even_admin(two_tenant_store):
    store, _ = two_tenant_store
    for role in ["public", "front_desk", "trainer", "location_manager", "hr", "finance", "marketing", "admin"]:
        titles = {d["title"] for d in store.list_documents(principal_for(role).access_filter())}
        assert B_TITLE not in titles, f"{role} could see Company B data"


def test_nft_gym_retrieval_only_returns_own_tenant(two_tenant_store):
    store, emb = two_tenant_store
    svc = RetrievalService(emb, store, LocalLLM(), top_k=8)
    for role in ["public", "admin"]:
        hits = svc.retrieve(principal_for(role), "opening hours and pricing")
        assert hits, f"{role} should still get NFT Gym results"
        assert all(h.chunk.meta["tenant_id"] == "nft_gym" for h in hits)


def test_company_b_principal_sees_only_company_b(two_tenant_store):
    store, _ = two_tenant_store
    docs = store.list_documents(principal_for("public", tenant="companyB").access_filter())
    assert {d["title"] for d in docs} == {B_TITLE}


def test_to_sql_always_prepends_tenant_clause():
    clause, params = AccessFilter("nft_gym", int(Classification.RESTRICTED), None, None).to_sql()
    assert clause.startswith("meta->>'tenant_id' = %s")
    assert params[0] == "nft_gym"


def test_missing_or_foreign_tenant_fails_closed():
    f = AccessFilter("nft_gym", int(Classification.RESTRICTED), None, None)
    assert f.allows({"classification": 0, "location": "global", "category": "general"}) is False  # no tenant
    assert f.allows({"tenant_id": "companyB", "classification": 0}) is False                        # other tenant
    assert f.allows({"tenant_id": "nft_gym", "classification": 0, "location": "global", "category": "general"}) is True

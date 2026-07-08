"""Retrieval respects the boundary: the model can't be handed forbidden chunks."""

from __future__ import annotations

import numpy as np

from app.retrieval.service import RetrievalService
from app.store.base import Chunk, Hit
from tests.conftest import principal_for


def categories(hits):
    return {h.chunk.meta["category"] for h in hits}


def test_front_desk_asking_about_salaries_gets_no_hr_chunks(service):
    hits = service.retrieve(principal_for("front_desk"), "What are the trainer salary bands?")
    assert "hr" not in categories(hits)  # restricted HR content is never retrieved


def test_hr_asking_about_salaries_does_get_hr_chunks(service):
    hits = service.retrieve(principal_for("hr"), "What are the trainer salary bands?")
    assert any(h.chunk.meta["category"] == "hr" for h in hits)


def test_public_gets_public_answer(service):
    hits = service.retrieve(principal_for("public"), "What are the opening hours?")
    assert hits
    assert all(h.chunk.meta["classification_label"] == "public" for h in hits)


def test_answer_stream_reports_efficiency_meta(service):
    events = list(service.answer_stream(principal_for("hr"), "What are the salary bands?"))
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["chunks_used"] <= 8          # bounded context, not the whole corpus
    assert events[-1]["type"] == "done"


def test_front_desk_salary_question_answers_with_no_access_message(service):
    events = list(service.answer_stream(principal_for("front_desk"), "trainer salary bands"))
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    assert "access" in answer.lower()


def test_greeting_answers_directly_without_retrieval():
    class ExplodingEmbedder:
        def embed_one(self, text):
            raise AssertionError("greetings should not be embedded")

    class ExplodingStore:
        def search(self, query, k, access):
            raise AssertionError("greetings should not search the vector store")

    class ExplodingLLM:
        name = "fake"
        model = "fake"

        def stream(self, question, hits, tenant_id="nft_gym", stats=None, history=None):
            raise AssertionError("greetings should not call the LLM")

    direct = RetrievalService(ExplodingEmbedder(), ExplodingStore(), ExplodingLLM())
    events = list(direct.answer_stream(principal_for("admin"), "hi"))

    answer = "".join(e["text"] for e in events if e["type"] == "token")
    sources = next(e for e in events if e["type"] == "sources")
    meta = next(e for e in events if e["type"] == "meta")

    assert answer.startswith("Hi!")
    assert sources["sources"] == []
    assert meta["chunks_used"] == 0
    assert meta["llm"] == "onebrain-direct"


def test_answer_stream_deduplicates_sources_by_document():
    meta = {
        "tenant_id": "nft_gym",
        "classification": 0,
        "classification_label": "public",
        "location": "global",
        "category": "general",
        "status": "approved",
        "doc_title": "nftgym.txt",
    }

    class StaticEmbedder:
        def embed_one(self, text):
            return np.array([1.0])

    class DuplicateStore:
        def search(self, query, k, access):
            hits = [
                Hit(Chunk("chunk_1", "doc_1", "Opening hours are 8 to 20.", dict(meta)), 0.91),
                Hit(Chunk("chunk_2", "doc_1", "Classes run daily.", dict(meta)), 0.82),
                Hit(
                    Chunk("chunk_3", "doc_2", "Memberships are monthly.", {**meta, "doc_title": "plans.txt"}),
                    0.77,
                ),
            ]
            return [h for h in hits if access.allows(h.chunk.meta)]

    class FakeLLM:
        name = "fake"
        model = "fake"

        def stream(self, question, hits, tenant_id="nft_gym", stats=None, history=None):
            yield "ok"

    deduped = RetrievalService(StaticEmbedder(), DuplicateStore(), FakeLLM())
    events = list(deduped.answer_stream(principal_for("public"), "What are the opening hours?"))
    sources = next(e for e in events if e["type"] == "sources")["sources"]
    meta_event = next(e for e in events if e["type"] == "meta")

    assert [s["title"] for s in sources] == ["nftgym.txt", "plans.txt"]
    assert sources[0]["chunks"] == 2
    assert meta_event["chunks_used"] == 3

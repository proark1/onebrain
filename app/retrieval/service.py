"""The gateway: embed -> permission-filter -> top-k -> generate.

This is where the whole answer efficiency lives: no matter how big the corpus,
only the top-k relevant chunks reach the model, so per-question token cost stays
roughly flat. The access filter is applied inside the store (unauthorised chunks
are never scored) and re-checked here before anything reaches the model.
"""

from __future__ import annotations

from typing import Iterator, List

from app.auth.principal import Principal
from app.store.base import Hit


def _estimate_tokens(hits: List[Hit], question: str) -> int:
    chars = sum(len(h.chunk.text) for h in hits) + len(question) + 400  # + system/instructions
    return round(chars / 4)


class RetrievalService:
    def __init__(self, embedder, store, llm, top_k: int = 8):
        self._embedder = embedder
        self._store = store
        self._llm = llm
        self._top_k = top_k

    def retrieve(self, principal: Principal, question: str) -> List[Hit]:
        query_vec = self._embedder.embed_one(question)
        access = principal.access_filter()
        hits = self._store.search(query_vec, self._top_k, access)
        # Defence in depth: re-check every returned chunk against the caller.
        return [h for h in hits if access.allows(h.chunk.meta)]

    def answer_stream(self, principal: Principal, question: str) -> Iterator[dict]:
        hits = self.retrieve(principal, question)

        for token in self._llm.stream(question, hits):
            yield {"type": "token", "text": token}

        yield {"type": "sources", "sources": [
            {
                "title": h.chunk.meta.get("doc_title", "Untitled"),
                "classification": h.chunk.meta.get("classification_label", "internal"),
                "location": h.chunk.meta.get("location", "global"),
                "category": h.chunk.meta.get("category", "general"),
                "score": round(h.score, 3),
            }
            for h in hits
        ]}
        yield {
            "type": "meta",
            "chunks_used": len(hits),
            "approx_tokens": _estimate_tokens(hits, question),
            "llm": self._llm.name,
        }
        yield {"type": "done"}

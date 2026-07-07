"""The gateway: embed -> permission-filter -> top-k -> generate.

This is where the whole answer efficiency lives: no matter how big the corpus,
only the top-k relevant chunks reach the model, so per-question token cost stays
roughly flat. The access filter is applied inside the store (unauthorised chunks
are never scored) and re-checked here before anything reaches the model.
"""

from __future__ import annotations

from typing import Iterator, List

from app.auth.principal import Principal
from app.llm.pricing import estimate_cost
from app.store.base import Hit


def _estimate_input_tokens(hits: List[Hit], question: str) -> int:
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

    def answer_stream(self, principal: Principal, question: str, history: list | None = None) -> Iterator[dict]:
        # For a follow-up, fold the previous user turn into the retrieval query
        # so "and the budget for it?" still finds the right chunks. Retrieval is
        # ALWAYS re-filtered by the current principal — history never widens access.
        retrieval_query = question
        if history:
            last_user = next((t["content"] for t in reversed(history) if t.get("role") == "user"), "")
            if last_user:
                retrieval_query = f"{last_user}\n{question}"
        hits = self.retrieve(principal, retrieval_query)

        stats: dict = {}
        answer_chars = 0
        for token in self._llm.stream(question, hits, principal.tenant_id, stats, history):
            answer_chars += len(token)
            yield {"type": "token", "text": token}

        # Never hand source metadata (doc titles, classification, location) to a
        # service principal — it would leak org structure to an external caller.
        # Stripped brain-side, before the response leaves, not at the edge.
        is_service = getattr(principal, "principal_type", "human") == "service"
        yield {"type": "sources", "sources": [] if is_service else [
            {
                "title": h.chunk.meta.get("doc_title", "Untitled"),
                "classification": h.chunk.meta.get("classification_label", "internal"),
                "location": h.chunk.meta.get("location", "global"),
                "category": h.chunk.meta.get("category", "general"),
                "score": round(h.score, 3),
            }
            for h in hits
        ]}

        # Prefer the model's real usage; fall back to a char-based estimate.
        input_tokens = stats.get("prompt_tokens") or _estimate_input_tokens(hits, question)
        output_tokens = stats.get("completion_tokens") or max(1, round(answer_chars / 4))
        model = getattr(self._llm, "model", self._llm.name)
        cost = stats.get("cost_usd")
        if cost is None:
            cost = estimate_cost(model, input_tokens, output_tokens)

        yield {
            "type": "meta",
            "chunks_used": len(hits),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost,
            "estimated": stats.get("prompt_tokens") is None,  # True when tokens are estimated, not measured
            "llm": self._llm.name,
        }
        yield {"type": "done"}

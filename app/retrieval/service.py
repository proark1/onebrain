"""The gateway: embed -> permission-filter -> top-k -> generate.

This is where the whole answer efficiency lives: no matter how big the corpus,
only the top-k relevant chunks reach the model, so per-question token cost stays
roughly flat. The access filter is applied inside the store (unauthorised chunks
are never scored) and re-checked here before anything reaches the model.
"""

from __future__ import annotations

import re
from typing import Iterator, List

from app.auth.principal import Principal
from app.llm.pricing import estimate_cost
from app.store.base import Hit


def _estimate_input_tokens(hits: List[Hit], question: str) -> int:
    chars = sum(len(h.chunk.text) for h in hits) + len(question) + 400  # + system/instructions
    return round(chars / 4)


_GREETING_RE = re.compile(
    r"^(hi|hello|hey|hiya|hallo|servus|moin|guten\s+(morgen|tag|abend)|"
    r"thanks|thank\s+you|danke|ok|okay|alles\s+klar)[.!?\s]*$",
    re.IGNORECASE,
)

HISTORY_QUERY_USER_TURNS = 3
HISTORY_QUERY_TURN_CHARS = 500


def _no_match_response() -> str:
    return (
        "I couldn't find anything relevant in the documents you can access about that. "
        "It may be restricted to another role, scoped to another workspace, below the "
        "retrieval confidence threshold, or simply not uploaded yet."
    )


def _direct_chat_response(question: str) -> str | None:
    text = " ".join(question.strip().lower().split())
    if not _GREETING_RE.match(text):
        return None
    if any(word in text for word in ("hallo", "guten", "servus", "moin", "danke", "alles klar")):
        return "Hallo! Was moechtest du ueber die Dokumente wissen, auf die du Zugriff hast?"
    if text.startswith("thank") or text.startswith("thanks"):
        return "You're welcome. What would you like to know from the documents you can access?"
    if text in {"ok", "okay"}:
        return "Okay. What would you like me to check in OneBrain?"
    return "Hi! What would you like to know from the documents you can access?"


def _source_records(hits: List[Hit]) -> list[dict]:
    sources: dict[tuple[str, str, str, str], dict] = {}
    for h in hits:
        key = (
            h.chunk.doc_id,
            h.chunk.meta.get("doc_title", "Untitled"),
            h.chunk.meta.get("classification_label", "internal"),
            h.chunk.meta.get("location", "global"),
        )
        source = sources.setdefault(key, {
            "doc_id": h.chunk.doc_id,
            "title": h.chunk.meta.get("doc_title", "Untitled"),
            "classification": h.chunk.meta.get("classification_label", "internal"),
            "location": h.chunk.meta.get("location", "global"),
            "category": h.chunk.meta.get("category", "general"),
            "score": round(h.score, 3),
            "chunks": 0,
        })
        source["chunks"] += 1
        source["score"] = max(source["score"], round(h.score, 3))
    return list(sources.values())


def _recent_user_turns(history: list | None) -> list[str]:
    turns: list[str] = []
    for turn in reversed(history or []):
        if turn.get("role") != "user":
            continue
        content = " ".join(str(turn.get("content") or "").split())
        if not content:
            continue
        turns.append(content[:HISTORY_QUERY_TURN_CHARS])
        if len(turns) >= HISTORY_QUERY_USER_TURNS:
            break
    return list(reversed(turns))


def _build_retrieval_query(question: str, history: list | None) -> tuple[str, int]:
    user_turns = _recent_user_turns(history)
    if not user_turns:
        return question, 0
    return "\n".join([*user_turns, question]), len(user_turns)


class RetrievalService:
    def __init__(self, embedder, store, llm, top_k: int = 8, min_score: float = 0.05):
        self._embedder = embedder
        self._store = store
        self._llm = llm
        self._top_k = top_k
        self._min_score = float(min_score)

    def retrieve(self, principal: Principal, question: str) -> List[Hit]:
        hits, _ = self._retrieve_with_meta(principal, question)
        return hits

    def _retrieve_with_meta(self, principal: Principal, question: str) -> tuple[List[Hit], dict]:
        query_vec = self._embedder.embed_one(question)
        access = principal.access_filter()
        raw_hits = self._store.search(query_vec, self._top_k, access)
        # Defence in depth: re-check every returned chunk against the caller.
        allowed = [h for h in raw_hits if access.allows(h.chunk.meta)]
        filtered = [h for h in allowed if h.score >= self._min_score]
        best_score = max((h.score for h in allowed), default=None)
        return filtered, {
            "retrieval_min_score": self._min_score,
            "best_score": round(best_score, 3) if best_score is not None else None,
            "filtered_chunks": len(allowed) - len(filtered),
        }

    def answer_stream(self, principal: Principal, question: str, history: list | None = None) -> Iterator[dict]:
        direct_response = _direct_chat_response(question)
        if direct_response is not None:
            yield {"type": "token", "text": direct_response}
            yield {"type": "sources", "sources": []}
            input_tokens = max(1, round(len(question) / 4))
            output_tokens = max(1, round(len(direct_response) / 4))
            yield {
                "type": "meta",
                "chunks_used": 0,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": 0,
                "estimated": False,
                "llm": "onebrain-direct",
                "retrieval_min_score": self._min_score,
                "best_score": None,
                "filtered_chunks": 0,
                "history_user_turns_used": 0,
            }
            yield {"type": "done"}
            return

        # For follow-ups, fold recent user turns into the retrieval query so
        # "and the budget for it?" still finds the right chunks. Retrieval is
        # ALWAYS re-filtered by the current principal — history never widens access.
        retrieval_query, history_turns_used = _build_retrieval_query(question, history)
        hits, retrieval_meta = self._retrieve_with_meta(principal, retrieval_query)
        retrieval_meta["history_user_turns_used"] = history_turns_used

        if not hits:
            response = _no_match_response()
            yield {"type": "token", "text": response}
            yield {"type": "sources", "sources": []}
            input_tokens = max(1, round(len(question) / 4))
            output_tokens = max(1, round(len(response) / 4))
            yield {
                "type": "meta",
                "chunks_used": 0,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": 0,
                "estimated": False,
                "llm": "onebrain-retrieval",
                **retrieval_meta,
            }
            yield {"type": "done"}
            return

        stats: dict = {}
        answer_chars = 0
        for token in self._llm.stream(question, hits, principal.tenant_id, stats, history):
            answer_chars += len(token)
            yield {"type": "token", "text": token}

        # Never hand source metadata (doc titles, classification, location) to a
        # service principal — it would leak org structure to an external caller.
        # Stripped brain-side, before the response leaves, not at the edge.
        is_service = getattr(principal, "principal_type", "human") == "service"
        yield {"type": "sources", "sources": [] if is_service else _source_records(hits)}

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
            **retrieval_meta,
        }
        yield {"type": "done"}

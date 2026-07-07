"""RAG prompt assembly — shared by every LLM backend.

Retrieved text is wrapped as numbered context and the model is told to answer
only from it. The context has already been permission-filtered, so the model
only ever sees what the caller is allowed to see.
"""

from __future__ import annotations

from typing import List

from app.store.base import Hit

SYSTEM_PROMPT = (
    "You are onebrain, the internal assistant for NFT Gym, a martial arts gym chain. "
    "Answer ONLY using the numbered context below. "
    "If the context does not contain the answer, say you don't have access to that "
    "information rather than guessing. Never invent facts. "
    "Cite sources inline as [1], [2]. Answer in the user's language."
)


def format_context(hits: List[Hit]) -> str:
    if not hits:
        return "(no accessible documents matched this question)"
    blocks = []
    for i, hit in enumerate(hits, 1):
        title = hit.chunk.meta.get("doc_title", "Untitled")
        blocks.append(f"[{i}] {title}\n{hit.chunk.text}")
    return "\n\n".join(blocks)


def build_messages(question: str, hits: List[Hit]) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n{format_context(hits)}\n\nQuestion: {question}"},
    ]

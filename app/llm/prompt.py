"""RAG prompt assembly — shared by every LLM backend.

Retrieved text is wrapped as numbered context and the model is told to answer
only from it. The context has already been permission-filtered, so the model
only ever sees what the caller is allowed to see.
"""

from __future__ import annotations

from typing import List

from app.store.base import Hit

# Per-tenant persona — a Company B channel user must never be told it's talking
# to NFT Gym's assistant. Add new tenants here as they onboard.
TENANT_PERSONAS = {
    "nft_gym": "onebrain, the assistant for NFT Gym, a martial arts gym chain",
}
_DEFAULT_PERSONA = "onebrain, a helpful assistant"


def _system_prompt(tenant_id: str) -> str:
    persona = TENANT_PERSONAS.get(tenant_id, _DEFAULT_PERSONA)
    return (
        f"You are {persona}. Answer ONLY using the numbered context below. "
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


def build_messages(question, hits, tenant_id="nft_gym", history=None):
    # Prior turns give the model conversational memory. We include only the
    # plain Q&A text of earlier turns (not their retrieved context) to keep it
    # cheap; the current turn carries freshly permission-filtered context.
    messages = [{"role": "system", "content": _system_prompt(tenant_id)}]
    for turn in (history or []):
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = (turn.get("content") or "")[:1500]
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": f"Context:\n{format_context(hits)}\n\nQuestion: {question}"})
    return messages

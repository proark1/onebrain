"""Real generation via LiteLLM — any provider behind one streaming interface.

Requires `pip install litellm` and the provider API key. For NFT Gym's higher
tiers this is where you'd route to an EU-sovereign / self-hosted model.
"""

from __future__ import annotations

from typing import Iterator, List

from app.llm.prompt import build_messages
from app.store.base import Hit


class LiteLLMLLM:
    name = "litellm"

    def __init__(self, model: str):
        import litellm

        self._litellm = litellm
        self.model = model

    def stream(self, question: str, hits: List[Hit], tenant_id: str = "nft_gym") -> Iterator[str]:
        response = self._litellm.completion(
            model=self.model, messages=build_messages(question, hits, tenant_id), stream=True
        )
        for part in response:
            delta = getattr(part.choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if content:
                yield content

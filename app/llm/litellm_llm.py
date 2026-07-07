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

    def stream(self, question, hits, tenant_id="nft_gym", stats=None):
        response = self._litellm.completion(
            model=self.model, messages=build_messages(question, hits, tenant_id),
            stream=True, stream_options={"include_usage": True},
        )
        usage = None
        for part in response:
            found = getattr(part, "usage", None)
            if found is not None:
                usage = found
            if part.choices:
                delta = getattr(part.choices[0], "delta", None)
                content = getattr(delta, "content", None)
                if content:
                    yield content

        if stats is not None and usage is not None:
            from app.llm.pricing import estimate_cost

            pt = getattr(usage, "prompt_tokens", None)
            ct = getattr(usage, "completion_tokens", None)
            stats["prompt_tokens"] = pt
            stats["completion_tokens"] = ct
            stats["cost_usd"] = estimate_cost(self.model, pt, ct)

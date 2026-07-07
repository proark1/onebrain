"""Rough per-request cost estimation.

List prices in USD per 1,000,000 tokens. These change often — edit here; nothing
else needs to change. Cost is shown as an estimate next to the token count.
"""

from __future__ import annotations

from typing import Optional

# model -> (input $/1M tokens, output $/1M tokens)
PRICING: dict[str, tuple[float, float]] = {
    "gemini/gemini-2.5-flash": (0.30, 2.50),
    "gemini/gemini-2.5-pro": (1.25, 10.00),
    "gemini/gemini-2.0-flash": (0.10, 0.40),
    "gemini/gemini-1.5-flash": (0.075, 0.30),
    "mistral/mistral-small-latest": (0.20, 0.60),
    "openai/gpt-4o-mini": (0.15, 0.60),
    # local no-key fallbacks are free
    "local": (0.0, 0.0),
    "local-extractive": (0.0, 0.0),
    "local-hashing": (0.0, 0.0),
}


def estimate_cost(model: str, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
    """USD cost estimate, or None if the model's pricing is unknown."""
    rates = PRICING.get(model)
    if rates is None or prompt_tokens is None:
        return None
    in_rate, out_rate = rates
    return (prompt_tokens * in_rate + (completion_tokens or 0) * out_rate) / 1_000_000

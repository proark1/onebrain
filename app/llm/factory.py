"""Pick the LLM from config."""

from __future__ import annotations

from app.config import Settings
from app.llm.local import LocalLLM


def build_llm(settings: Settings):
    if settings.llm_provider == "litellm":
        from app.llm.litellm_llm import LiteLLMLLM

        default = LiteLLMLLM(settings.litellm_model)

        # Wrap in the per-tier router only when sovereign routing is configured or
        # explicitly required; otherwise return the plain default (no behaviour change).
        if settings.sovereign_llm_model or settings.sovereign_required:
            from app.llm.tiered import TieredLLM
            from app.security.policy import Classification

            sovereign = LiteLLMLLM(settings.sovereign_llm_model) if settings.sovereign_llm_model else None
            threshold = Classification.parse(settings.sovereign_min_tier)
            return TieredLLM(default, sovereign, threshold, settings.sovereign_required)
        return default
    return LocalLLM()

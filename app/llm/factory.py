"""Pick the LLM from config."""

from __future__ import annotations

from app.config import Settings
from app.llm.local import LocalLLM


def build_llm(settings: Settings):
    if settings.llm_provider == "litellm":
        from app.llm.litellm_llm import LiteLLMLLM

        return LiteLLMLLM(settings.litellm_model)
    return LocalLLM()

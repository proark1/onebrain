"""Build configuration-gated model backends for AI Employees."""

from __future__ import annotations

import os

from app.ai_employees.backends.litellm import LiteLLMAgentBackend
from app.ai_employees.backends.local import LocalAgentBackend
from app.ai_employees.backends.registry import BackendRegistry
from app.config import Settings


def build_ai_employee_backend_registry(settings: Settings) -> BackendRegistry:
    litellm_ready = settings.llm_provider == "litellm"
    gemini = LiteLLMAgentBackend(
        "gemini",
        available=litellm_ready,
        unavailable_reason="LiteLLM/Gemini is not configured." if not litellm_ready else "",
    )
    anthropic_credential = bool(os.environ.get("ANTHROPIC_API_KEY"))
    anthropic_ready = (
        litellm_ready
        and settings.ai_employees_anthropic_enabled
        and settings.ai_employees_anthropic_processing_approved
        and settings.ai_employees_code_sandbox_enabled
        and anthropic_credential
    )
    anthropic = LiteLLMAgentBackend(
        "anthropic",
        available=anthropic_ready,
        unavailable_reason=(
            "Anthropic credentials, processing approval, and the isolated coding sandbox are required."
            if not anthropic_ready else ""
        ),
    )
    return BackendRegistry([gemini, anthropic, LocalAgentBackend()])

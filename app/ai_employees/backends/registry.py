"""Explicit, fail-closed AI employee backend selection."""

from __future__ import annotations

from app.ai_employees.backends.base import AgentBackend, BackendUnavailableError
from app.ai_employees.base import AiEmployeeModelPolicy
from app.ai_employees.contracts import validate_ai_employee_provider
from app.security.policy import Classification


_FALLBACK_MODELS = {
    "gemini": "gemini/gemini-2.5-flash",
    "anthropic": "anthropic/claude-sonnet-4-5",
    "local": "local",
}


class BackendRegistry:
    def __init__(self, backends=()):
        self._backends: dict[str, AgentBackend] = {}
        for backend in backends:
            self.register(backend)

    def register(self, backend: AgentBackend) -> None:
        provider = validate_ai_employee_provider(backend.provider)
        self._backends[provider] = backend

    def resolve(
        self,
        policy: AiEmployeeModelPolicy,
        classification: Classification,
    ) -> tuple[AgentBackend, str]:
        if policy.status != "active":
            raise BackendUnavailableError("The AI employee model policy is inactive.")
        ceiling = Classification.parse(policy.data_ceiling)
        classification = Classification.parse(classification)
        if classification > ceiling:
            raise BackendUnavailableError(
                f"The requested data exceeds the model policy data ceiling ({policy.data_ceiling})."
            )

        candidates = (policy.provider, *policy.allowed_fallbacks)
        reasons: list[str] = []
        for index, candidate in enumerate(candidates):
            provider = validate_ai_employee_provider(candidate)
            backend = self._backends.get(provider)
            if not backend:
                reasons.append(f"{provider}: backend not registered")
                continue
            if not backend.available:
                reasons.append(f"{provider}: {backend.unavailable_reason or 'not available'}")
                continue
            model = policy.model if index == 0 else _FALLBACK_MODELS[provider]
            return backend, model
        detail = "; ".join(reasons) or "no approved backend configured"
        raise BackendUnavailableError(f"No approved AI employee backend is available: {detail}.")

    def health(self) -> list[dict[str, str | bool]]:
        return [
            {
                "provider": provider,
                "available": bool(backend.available),
                "reason": backend.unavailable_reason,
            }
            for provider, backend in sorted(self._backends.items())
        ]

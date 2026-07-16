"""Deterministic no-network backend for explicit local policies and tests."""

from __future__ import annotations

from app.ai_employees.backends.base import AgentBackendRequest, BackendEvent


class LocalAgentBackend:
    provider = "local"
    available = True
    unavailable_reason = ""

    def stream(self, request: AgentBackendRequest):
        answer = (
            "This AI employee is using the local fallback. I can organize the approved "
            "context, but a configured model is required for a full response."
        )
        yield BackendEvent(type="text", text=answer)
        yield BackendEvent(
            type="usage",
            prompt_tokens=max(1, sum(len(row.get("content", "")) for row in request.messages) // 4),
            completion_tokens=max(1, len(answer) // 4),
            cost_usd=0.0,
        )
        yield BackendEvent(type="done")

"""Provider-neutral backend routing contracts for AI Employees."""

from __future__ import annotations

import pytest

from app.ai_employees.backends.base import (
    AgentBackendRequest,
    BackendEvent,
    BackendUnavailableError,
)
from app.ai_employees.backends.registry import BackendRegistry
from app.ai_employees.base import AiEmployeeModelPolicy
from app.security.policy import Classification


class _Backend:
    def __init__(self, provider: str, *, available: bool = True):
        self.provider = provider
        self.available = available
        self.unavailable_reason = "not configured" if not available else ""
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        yield BackendEvent(type="text", text="hello")
        yield BackendEvent(
            type="usage", prompt_tokens=12, completion_tokens=3, cost_usd=0.001,
            provider_session_ref="provider-run-1",
        )
        yield BackendEvent(type="done")


def _policy(**changes):
    values = {
        "id": "policy-1",
        "tenant_id": "acme",
        "account_id": "acme",
        "space_id": "business",
        "employee_id": "chief_of_staff",
        "version": 1,
        "provider": "gemini",
        "model": "gemini/gemini-2.5-flash",
        "task_overrides": {},
        "allowed_fallbacks": (),
        "data_ceiling": "internal",
        "cost_limit_usd": 5.0,
        "status": "active",
    }
    values.update(changes)
    return AiEmployeeModelPolicy(**values)


def test_backend_event_contract_and_registry_resolve_gemini():
    backend = _Backend("gemini")
    registry = BackendRegistry([backend])

    resolved, model = registry.resolve(_policy(), Classification.INTERNAL)
    assert resolved is backend
    assert model == "gemini/gemini-2.5-flash"
    events = list(resolved.stream(AgentBackendRequest(
        model=model,
        messages=({"role": "system", "content": "policy"},),
        max_output_tokens=1000,
    )))
    assert [event.type for event in events] == ["text", "usage", "done"]
    assert events[1].prompt_tokens == 12
    assert events[1].provider_session_ref == "provider-run-1"


def test_registry_fails_closed_for_data_ceiling_unavailable_provider_and_unapproved_fallback():
    gemini = _Backend("gemini", available=False)
    local = _Backend("local")
    registry = BackendRegistry([gemini, local])

    with pytest.raises(BackendUnavailableError, match="data ceiling"):
        registry.resolve(_policy(), Classification.CONFIDENTIAL)
    with pytest.raises(BackendUnavailableError, match="not configured"):
        registry.resolve(_policy(), Classification.INTERNAL)

    fallback_policy = _policy(allowed_fallbacks=("local",))
    resolved, model = registry.resolve(fallback_policy, Classification.INTERNAL)
    assert resolved is local
    assert model == "local"


def test_registry_rejects_anthropic_until_the_backend_is_explicitly_available():
    registry = BackendRegistry([_Backend("anthropic", available=False)])
    with pytest.raises(BackendUnavailableError, match="not configured"):
        registry.resolve(
            _policy(provider="anthropic", model="anthropic/claude-sonnet-4-5", data_ceiling="internal"),
            Classification.INTERNAL,
        )


def test_backend_request_rejects_unbounded_or_empty_input():
    with pytest.raises(ValueError, match="messages"):
        AgentBackendRequest(model="gemini/gemini-2.5-flash", messages=(), max_output_tokens=100)
    with pytest.raises(ValueError, match="max_output_tokens"):
        AgentBackendRequest(
            model="gemini/gemini-2.5-flash",
            messages=({"role": "system", "content": "policy"},),
            max_output_tokens=0,
        )

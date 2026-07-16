"""Persistent direct employee conversation runtime contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.ai_employees.backends.base import BackendEvent
from app.ai_employees.backends.registry import BackendRegistry
from app.ai_employees.memory import MemoryAiEmployeeStore
from app.ai_employees.runtime import AiEmployeeRuntime
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.store.base import Chunk, Hit


class _Retrieval:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.calls = []

    def retrieve(self, principal, question):
        self.calls.append((principal, question))
        return list(self.hits)


class _Backend:
    provider = "gemini"
    available = True
    unavailable_reason = ""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("raw provider response with SECRET-123")
        yield BackendEvent(type="text", text="The current evidence shows ")
        yield BackendEvent(type="text", text="a 12-month runway [1].")
        yield BackendEvent(
            type="usage", prompt_tokens=120, completion_tokens=20,
            cost_usd=0.0001, provider_session_ref="gemini-interaction-1",
        )
        yield BackendEvent(type="done")


def _principal(user_id: str = "admin@acme") -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id=user_id, role_id=role.id, role_label=role.label,
        clearance=role.clearance, locations=None, categories=role.categories,
        location_label="all", tenant_id="acme", account_id="acme",
        space_ids=frozenset({"business"}),
    )


def _runtime(*, backend=None, hits=None):
    store = MemoryAiEmployeeStore()
    store.seed_defaults(
        tenant_id="acme", account_id="acme", space_id="business", author_id="system:test",
    )
    backend = backend or _Backend()
    retrieval = _Retrieval(hits or [Hit(Chunk(
        id="chunk-1", doc_id="doc-1", text="Approved plan: runway is 12 months.",
        meta={
            "tenant_id": "acme", "account_id": "acme", "space_id": "business",
            "classification": 1, "classification_label": "internal", "doc_title": "Plan",
            "status": "approved",
        },
    ), 0.95)])
    runtime = AiEmployeeRuntime(
        store=store,
        retrieval_service=retrieval,
        backend_registry=BackendRegistry([backend]),
        max_output_tokens=1000,
    )
    return runtime, store, retrieval, backend


def test_direct_turn_pins_versions_streams_and_persists_citations_and_usage():
    runtime, store, retrieval, backend = _runtime()
    principal = _principal()
    conversation = runtime.create_conversation(
        principal=principal,
        account_id="acme",
        space_id="business",
        employee_id="finance_manager",
        title="Runway review",
    )

    events = list(runtime.stream_turn(
        principal=principal,
        conversation_id=conversation.id,
        question="How much runway do we have?",
        idempotency_key="turn-1",
    ))

    assert [event["type"] for event in events] == ["run", "text", "text", "sources", "usage", "done"]
    assert events[0]["employee_id"] == "finance_manager"
    assert events[-2]["prompt_tokens"] == 120
    assert events[-2]["provider_session_ref"] == "gemini-interaction-1"
    assert events[-1]["replayed"] is False
    assert retrieval.calls[0][1] == "How much runway do we have?"
    assert backend.requests[0].model == "gemini/gemini-2.5-flash"
    assert "Sophie Laurent" in backend.requests[0].messages[0]["content"]

    messages = store.list_messages(
        conversation.id, tenant_id="acme", account_id="acme", space_id="business",
    )
    assert [message.speaker_type for message in messages] == ["human", "employee"]
    assert messages[1].citations == ("doc-1",)
    run = store.get_run_by_idempotency(
        "turn-1", tenant_id="acme", account_id="acme", space_id="business",
    )
    assert run.status == "completed"
    assert run.prompt_tokens == 120
    assert run.completion_tokens == 20
    assert run.provider_session_ref == "gemini-interaction-1"


def test_idempotent_turn_replays_the_stored_result_without_calling_the_model_twice():
    runtime, _, _, backend = _runtime()
    principal = _principal()
    conversation = runtime.create_conversation(
        principal=principal, account_id="acme", space_id="business",
        employee_id="chief_of_staff", title="Weekly priorities",
    )
    first = list(runtime.stream_turn(
        principal=principal, conversation_id=conversation.id,
        question="What matters this week?", idempotency_key="same-turn",
    ))
    replay = list(runtime.stream_turn(
        principal=principal, conversation_id=conversation.id,
        question="What matters this week?", idempotency_key="same-turn",
    ))

    assert first[-1]["replayed"] is False
    assert replay[-1]["replayed"] is True
    assert len(backend.requests) == 1
    assert "12-month runway" in "".join(event.get("text", "") for event in replay)


def test_direct_turn_rejects_other_owner_scope_paused_employee_and_conflicting_replay():
    runtime, store, _, _ = _runtime()
    principal = _principal()
    conversation = runtime.create_conversation(
        principal=principal, account_id="acme", space_id="business",
        employee_id="finance_manager", title="Finance",
    )
    with pytest.raises(PermissionError, match="owner"):
        list(runtime.stream_turn(
            principal=_principal("other@acme"), conversation_id=conversation.id,
            question="Show me", idempotency_key="other-turn",
        ))

    profile = store.get_profile(
        "finance_manager", tenant_id="acme", account_id="acme", space_id="business",
    )
    store.save_profile(replace(profile, status="paused"))
    with pytest.raises(ValueError, match="paused"):
        list(runtime.stream_turn(
            principal=principal, conversation_id=conversation.id,
            question="Show me", idempotency_key="paused-turn",
        ))

    store.save_profile(replace(store.get_profile(
        "finance_manager", tenant_id="acme", account_id="acme", space_id="business",
    ), status="active"))
    list(runtime.stream_turn(
        principal=principal, conversation_id=conversation.id,
        question="First", idempotency_key="conflict-turn",
    ))
    with pytest.raises(ValueError, match="idempotency"):
        list(runtime.stream_turn(
            principal=principal, conversation_id=conversation.id,
            question="Different", idempotency_key="conflict-turn",
        ))


def test_provider_failure_is_persisted_and_streamed_without_raw_provider_details():
    runtime, store, _, _ = _runtime(backend=_Backend(fail=True))
    principal = _principal()
    conversation = runtime.create_conversation(
        principal=principal, account_id="acme", space_id="business",
        employee_id="finance_manager", title="Finance",
    )
    events = list(runtime.stream_turn(
        principal=principal, conversation_id=conversation.id,
        question="Prepare the report", idempotency_key="failed-turn",
    ))
    assert events[-2] == {
        "type": "error",
        "code": "backend_failed",
        "message": "The AI employee could not complete this turn.",
    }
    assert events[-1]["type"] == "done"
    assert "SECRET-123" not in str(events)
    run = store.get_run_by_idempotency(
        "failed-turn", tenant_id="acme", account_id="acme", space_id="business",
    )
    assert run.status == "failed"
    assert run.error == "AI employee backend failed."

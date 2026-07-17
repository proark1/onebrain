"""Direct-turn lease, idempotency, and cancellation guarantees."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.ai_employees.backends.base import BackendEvent
from app.ai_employees.backends.registry import BackendRegistry
from app.ai_employees.base import (
    AI_AGENT_RUN_LEASE_EXPIRED_ERROR,
    AiAgentRun,
    AiEmployeeConversation,
    AiEmployeeMessage,
    now_iso,
)
from app.ai_employees.memory import MemoryAiEmployeeStore
from app.ai_employees.runtime import AiEmployeeRuntime
from app.auth.principal import Principal
from app.auth.roles import ROLES


SCOPE = {"tenant_id": "acme", "account_id": "acme", "space_id": "business"}


class _Retrieval:
    def retrieve(self, principal, question):
        return []


class _Backend:
    provider = "gemini"
    available = True
    unavailable_reason = ""

    def __init__(self):
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        yield BackendEvent(type="text", text="A governed answer.")
        yield BackendEvent(type="usage", prompt_tokens=8, completion_tokens=4, cost_usd=0.0001)
        yield BackendEvent(type="done")


def _principal() -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id="admin@acme",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
        tenant_id="acme",
        account_id="acme",
        space_ids=frozenset({"business"}),
    )


def _runtime():
    store = MemoryAiEmployeeStore()
    store.seed_defaults(**SCOPE, author_id="system:test")
    backend = _Backend()
    runtime = AiEmployeeRuntime(
        store=store,
        retrieval_service=_Retrieval(),
        backend_registry=BackendRegistry([backend]),
        run_lease_seconds=120,
        run_heartbeat_seconds=10,
        provider_timeout_seconds=20,
    )
    conversation = runtime.create_conversation(
        principal=_principal(),
        account_id="acme",
        space_id="business",
        employee_id="finance_manager",
        title="Lease testing",
    )
    return runtime, store, backend, conversation


def _new_run(*, conversation: AiEmployeeConversation, run_id: str, token: str, expires_at: str) -> AiAgentRun:
    return AiAgentRun(
        id=run_id,
        **SCOPE,
        conversation_id=conversation.id,
        mission_id="",
        employee_id=conversation.employee_id,
        backend="gemini",
        model="gemini/gemini-2.5-flash",
        idempotency_key="lease-key",
        status="running",
        input_hash="input-hash",
        lease_token=token,
        heartbeat_at=now_iso(),
        lease_expires_at=expires_at,
        started_at=now_iso(),
    )


def _human_message(run: AiAgentRun) -> AiEmployeeMessage:
    return AiEmployeeMessage(
        id=f"message-{run.id}",
        **SCOPE,
        conversation_id=run.conversation_id,
        speaker_type="human",
        speaker_id="admin@acme",
        visibility="shared",
        content="Question",
        run_id=run.id,
    )


def test_competing_direct_turn_is_replayed_and_generator_close_cancels_the_owned_run():
    runtime, store, backend, conversation = _runtime()
    principal = _principal()

    first = runtime.stream_turn(
        principal=principal,
        conversation_id=conversation.id,
        question="What should happen next?",
        idempotency_key="one-paid-turn",
    )
    started = next(first)
    assert started["type"] == "run"

    backend.available = False
    backend.unavailable_reason = "temporarily unavailable"
    competing = list(runtime.stream_turn(
        principal=principal,
        conversation_id=conversation.id,
        question="What should happen next?",
        idempotency_key="one-paid-turn",
    ))
    assert [event["type"] for event in competing] == ["run", "error", "done"]
    assert competing[1]["code"] == "turn_in_progress"
    assert backend.requests == []

    # StreamingResponse closes this nested generator on client disconnect.
    first.close()
    run = store.get_run_by_idempotency("one-paid-turn", **SCOPE)
    assert run.status == "cancelled"
    assert run.lease_token == ""
    assert [row.speaker_type for row in store.list_messages(conversation.id, **SCOPE)] == ["human"]

    replay = list(runtime.stream_turn(
        principal=principal,
        conversation_id=conversation.id,
        question="What should happen next?",
        idempotency_key="one-paid-turn",
    ))
    assert replay[1]["code"] == "turn_cancelled"
    assert backend.requests == []


def test_expired_run_is_terminal_without_a_paid_retry_and_stale_owner_cannot_finalize():
    runtime, store, _, conversation = _runtime()
    del runtime
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    run = _new_run(conversation=conversation, run_id="run-1", token="owner-a", expires_at=future)
    first = store.begin_or_get_run(run, human_message=_human_message(run))
    assert first.acquired is True
    assert store.export_scope(**SCOPE)["runs"][0]["lease_token"] == ""
    duplicate = _new_run(
        conversation=conversation,
        run_id="run-duplicate",
        token="owner-b",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    duplicate_claim = store.begin_or_get_run(duplicate, human_message=_human_message(duplicate))
    assert duplicate_claim.acquired is False
    assert duplicate_claim.run.id == run.id
    assert len(store.list_runs(**SCOPE)) == 1

    renewed = store.heartbeat_run(
        run.id,
        **SCOPE,
        lease_token="owner-a",
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat(),
    )
    assert renewed is not None
    assert renewed.lease_token == "owner-a"

    # Simulate the persisted record left behind after a crashed owner. The
    # public store API deliberately refuses stale lease updates.
    with store._lock:
        store._tables["runs"][run.id] = replace(
            renewed,
            lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )

    contender = _new_run(
        conversation=conversation,
        run_id="run-2",
        token="owner-b",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    result = store.begin_or_get_run(contender, human_message=_human_message(contender))
    assert result.acquired is False
    assert result.run.id == run.id
    assert result.run.status == "failed"
    assert result.run.error == AI_AGENT_RUN_LEASE_EXPIRED_ERROR
    assert result.run.lease_token == ""
    assert store.finalize_owned_run(
        replace(run, status="completed", completed_at=now_iso()),
        lease_token="owner-a",
    ) is None
    assert [row.speaker_type for row in store.list_messages(conversation.id, **SCOPE)] == ["human"]


def test_legacy_unleased_direct_turn_is_terminalized_instead_of_retried():
    runtime, store, _, conversation = _runtime()
    del runtime
    legacy = replace(
        _new_run(
            conversation=conversation,
            run_id="legacy-run",
            token="legacy-owner",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        ),
        lease_token="",
        lease_expires_at="",
        heartbeat_at="",
    )
    store.save_run(legacy)

    contender = _new_run(
        conversation=conversation,
        run_id="new-run",
        token="owner-b",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    result = store.begin_or_get_run(contender, human_message=_human_message(contender))

    assert result.acquired is False
    assert result.run.id == legacy.id
    assert result.run.status == "failed"
    assert result.run.error == AI_AGENT_RUN_LEASE_EXPIRED_ERROR


def test_runtime_passes_the_configured_bounded_provider_timeout():
    runtime, _, backend, conversation = _runtime()

    events = list(runtime.stream_turn(
        principal=_principal(),
        conversation_id=conversation.id,
        question="Give the concise answer.",
        idempotency_key="timeout-bound",
    ))

    assert events[-1]["type"] == "done"
    assert backend.requests[0].timeout_seconds == 20

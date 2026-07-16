"""Bounded multi-agent mission state-machine contracts."""

from __future__ import annotations

from dataclasses import replace

from app.ai_employees.memory import MemoryAiEmployeeStore
from app.ai_employees.missions import AiMissionService, MissionAgentResult
from app.auth.principal import Principal
from app.auth.roles import ROLES


class Retrieval:
    def retrieve(self, principal, question):
        return []


class Executor:
    def __init__(self, *, fail=(), tokens: int = 100):
        self.fail = set(fail)
        self.tokens = tokens
        self.calls = []

    def generate(self, request):
        key = (request.phase, request.employee_id)
        self.calls.append((request.phase, request.employee_id, tuple(request.context)))
        if key in self.fail:
            raise RuntimeError("raw provider error")
        return MissionAgentResult(
            content=f"{request.phase} from {request.employee_id}",
            prompt_tokens=self.tokens,
            completion_tokens=self.tokens,
            cost_usd=0.01,
            backend="gemini",
            model="gemini/gemini-2.5-flash",
        )


def _principal():
    role = ROLES["admin"]
    return Principal(
        user_id="admin@acme", role_id=role.id, role_label=role.label,
        clearance=role.clearance, locations=None, categories=role.categories,
        location_label="all", tenant_id="acme", account_id="acme",
        space_ids=frozenset({"business"}),
    )


def _service(*, executor=None):
    store = MemoryAiEmployeeStore()
    store.seed_defaults(
        tenant_id="acme", account_id="acme", space_id="business", author_id="system:test",
    )
    executor = executor or Executor()
    return AiMissionService(store=store, retrieval_service=Retrieval(), executor=executor), store, executor


def _create(service, **changes):
    values = {
        "principal": _principal(),
        "account_id": "acme",
        "space_id": "business",
        "goal": "Prepare the next-quarter operating plan.",
        "accountable_employee_id": "chief_operating_officer",
        "participant_ids": (
            "chief_of_staff", "chief_operating_officer", "finance_manager",
        ),
        "token_budget": 20_000,
        "time_budget_seconds": 600,
        "cost_budget_usd": 5.0,
    }
    values.update(changes)
    return service.create_mission(**values)


def test_mission_runs_separate_scope_positions_challenge_plan_and_synthesis_turns():
    service, store, executor = _service()
    mission, conversation = _create(service)
    events = list(service.run_mission(principal=_principal(), mission_id=mission.id))

    assert [(phase, employee) for phase, employee, _ in executor.calls] == [
        ("scope", "chief_of_staff"),
        ("position", "chief_operating_officer"),
        ("position", "finance_manager"),
        ("challenge", "chief_operating_officer"),
        ("challenge", "finance_manager"),
        ("accountable_plan", "chief_operating_officer"),
        ("synthesis", "chief_of_staff"),
    ]
    position_calls = [call for call in executor.calls if call[0] == "position"]
    assert all(len(context) == 1 for _, _, context in position_calls)
    assert all(context[0]["speaker"] == "chief_of_staff" for _, _, context in position_calls)
    assert events[-1]["type"] == "mission_done"
    assert events[-1]["incomplete"] is False
    completed = store.get_mission(
        mission.id, tenant_id="acme", account_id="acme", space_id="business",
    )
    assert completed.status == "completed"
    assert completed.phase == "human_review"
    assert completed.synthesis_message_id
    messages = store.list_messages(
        conversation.id, tenant_id="acme", account_id="acme", space_id="business",
    )
    assert len(messages) == 7
    assert {message.speaker_id for message in messages} == {
        "chief_of_staff", "chief_operating_officer", "finance_manager",
    }


def test_optional_specialist_failure_completes_incomplete_but_accountable_failure_pauses():
    service, store, _ = _service(executor=Executor(fail={("position", "finance_manager")}))
    mission, _ = _create(service)
    events = list(service.run_mission(principal=_principal(), mission_id=mission.id))
    assert events[-1]["type"] == "mission_done"
    assert events[-1]["incomplete"] is True
    assert "finance_manager:position" in events[-1]["missing_inputs"]
    assert store.get_mission(
        mission.id, tenant_id="acme", account_id="acme", space_id="business",
    ).status == "completed"

    service2, store2, _ = _service(executor=Executor(fail={("accountable_plan", "chief_operating_officer")}))
    mission2, _ = _create(service2)
    events2 = list(service2.run_mission(principal=_principal(), mission_id=mission2.id))
    assert events2[-1]["type"] == "mission_paused"
    assert "Accountable executive" in events2[-1]["reason"]
    assert store2.get_mission(
        mission2.id, tenant_id="acme", account_id="acme", space_id="business",
    ).status == "paused"


def test_mission_retries_reuse_completed_turns_and_budget_exhaustion_pauses():
    executor = Executor(tokens=200)
    service, store, _ = _service(executor=executor)
    mission, _ = _create(service, token_budget=1_000)
    events = list(service.run_mission(principal=_principal(), mission_id=mission.id))
    assert events[-1]["type"] == "mission_paused"
    assert events[-1]["reason"] == "Mission budget exhausted."
    first_call_count = len(executor.calls)

    # Raise the persisted budget and resume. Completed phase turns replay from
    # durable runs instead of calling the model again.
    current = store.get_mission(
        mission.id, tenant_id="acme", account_id="acme", space_id="business",
    )
    store.save_mission(replace(current, token_budget=20_000, status="queued"))
    resumed = list(service.run_mission(principal=_principal(), mission_id=mission.id))
    assert resumed[-1]["type"] == "mission_done"
    assert len(executor.calls) < first_call_count + 7


def test_mission_cancellation_is_durable_and_completed_mission_cannot_be_cancelled():
    service, store, _ = _service()
    mission, _ = _create(service)
    cancelled = service.cancel_mission(principal=_principal(), mission_id=mission.id)
    assert cancelled.status == "cancelled"
    assert list(service.run_mission(principal=_principal(), mission_id=mission.id))[-1]["status"] == "cancelled"

    mission2, _ = _create(service, goal="A second mission")
    list(service.run_mission(principal=_principal(), mission_id=mission2.id))
    try:
        service.cancel_mission(principal=_principal(), mission_id=mission2.id)
    except ValueError as exc:
        assert "cannot be cancelled" in str(exc)
    else:
        raise AssertionError("Completed mission cancellation should fail")

"""Bounded, durable multi-agent mission orchestration."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from uuid import uuid4

from app.ai_employees.backends.base import AgentBackendRequest
from app.ai_employees.base import (
    AiAgentRun,
    AiEmployeeConversation,
    AiEmployeeMessage,
    AiMission,
    AiMissionParticipant,
    now_iso,
)
from app.ai_employees.contracts import (
    LEADERSHIP_COUNCIL_IDS,
    get_ai_employee,
    validate_mission_squad,
)
from app.ai_employees.memory_service import active_approved_memories
from app.ai_employees.prompting import compile_agent_messages
from app.security.policy import Classification


ACCOUNTABLE_EXECUTIVE_IDS = frozenset(LEADERSHIP_COUNCIL_IDS)


@dataclass(frozen=True)
class MissionAgentRequest:
    mission: AiMission
    employee_id: str
    phase: str
    instruction: str
    context: tuple[dict[str, str], ...]
    character_payload: dict
    policy: object
    memories: tuple
    hits: tuple


@dataclass(frozen=True)
class MissionAgentResult:
    content: str
    citations: tuple[str, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    provider_session_ref: str = ""
    backend: str = ""
    model: str = ""
    warning: str = ""


class MissionTurnError(RuntimeError):
    """A bounded employee turn failed without exposing provider details."""


class ModelMissionAgentExecutor:
    def __init__(self, backend_registry, *, max_output_tokens: int = 2_048):
        self.backend_registry = backend_registry
        self.max_output_tokens = max(1, min(int(max_output_tokens), 32_768))

    def generate(self, request: MissionAgentRequest) -> MissionAgentResult:
        employee = get_ai_employee(request.employee_id)
        classification = _max_classification(request.hits)
        backend, model = self.backend_registry.resolve(request.policy, classification)
        messages = compile_agent_messages(
            employee=employee,
            character_payload=request.character_payload,
            question=request.instruction,
            conversation=list(request.context),
            memories=list(request.memories),
            hits=list(request.hits),
            assignment=f"Mission {request.mission.id} / {request.phase}: {request.mission.goal}",
            token_budget=request.mission.token_budget,
            cost_budget_usd=request.mission.cost_budget_usd,
        )
        answer_parts: list[str] = []
        usage = MissionAgentResult(content="", backend=backend.provider, model=model)
        warning = ""
        for event in backend.stream(AgentBackendRequest(
            model=model,
            messages=messages,
            max_output_tokens=self.max_output_tokens,
        )):
            if event.type == "text":
                answer_parts.append(event.text)
            elif event.type == "usage":
                usage = MissionAgentResult(
                    content="",
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    cost_usd=float(event.cost_usd or 0.0),
                    provider_session_ref=event.provider_session_ref,
                    backend=backend.provider,
                    model=model,
                )
            elif event.type == "warning":
                warning = event.text[:500]
            elif event.type in {"tool_request", "error"}:
                raise MissionTurnError("AI employee mission turn failed.")
        content = "".join(answer_parts).strip()
        if not content:
            raise MissionTurnError("AI employee mission turn returned no content.")
        return replace(
            usage,
            content=content,
            citations=tuple(dict.fromkeys(hit.chunk.doc_id for hit in request.hits)),
            warning=warning,
        )


class AiMissionService:
    def __init__(self, *, store, retrieval_service, executor):
        self.store = store
        self.retrieval_service = retrieval_service
        self.executor = executor

    def create_mission(
        self,
        *,
        principal,
        account_id: str,
        space_id: str,
        goal: str,
        accountable_employee_id: str,
        participant_ids: tuple[str, ...],
        token_budget: int = 30_000,
        time_budget_seconds: int = 900,
        cost_budget_usd: float = 10.0,
    ) -> tuple[AiMission, AiEmployeeConversation]:
        participant_ids = validate_mission_squad(participant_ids)
        if accountable_employee_id not in participant_ids:
            raise ValueError("The accountable executive must participate in the mission squad.")
        if accountable_employee_id not in ACCOUNTABLE_EXECUTIVE_IDS:
            raise ValueError("The accountable mission owner must be a leadership council executive.")
        goal = (goal or "").strip()
        if not 1 <= len(goal) <= 4_000:
            raise ValueError("AI employee mission goal must contain 1 to 4000 characters.")
        if not 1_000 <= token_budget <= 500_000:
            raise ValueError("AI employee mission token budget must be between 1000 and 500000.")
        if not 30 <= time_budget_seconds <= 86_400:
            raise ValueError("AI employee mission time budget must be between 30 and 86400 seconds.")
        if not 0 < cost_budget_usd <= 1_000:
            raise ValueError("AI employee mission cost budget must be greater than 0 and at most 1000 USD.")
        scope = {
            "tenant_id": principal.tenant_id,
            "account_id": account_id,
            "space_id": space_id,
        }
        profiles = {}
        policies = {}
        for employee_id in participant_ids:
            profile = self.store.get_profile(employee_id, **scope)
            policy = self.store.get_model_policy(employee_id, **scope)
            if not profile or profile.status != "active" or not policy:
                raise ValueError(f"AI employee is paused or not configured: {employee_id}")
            profiles[employee_id] = profile
            policies[employee_id] = policy

        mission = self.store.save_mission(AiMission(
            id=f"aimission_{uuid4().hex}",
            **scope,
            goal=goal,
            sponsor_id=principal.user_id,
            accountable_employee_id=accountable_employee_id,
            status="draft",
            phase="scope",
            token_budget=token_budget,
            time_budget_seconds=time_budget_seconds,
            cost_budget_usd=cost_budget_usd,
        ))
        clara = profiles["chief_of_staff"]
        clara_policy = policies["chief_of_staff"]
        conversation = self.store.save_conversation(AiEmployeeConversation(
            id=f"aic_{uuid4().hex}",
            **scope,
            employee_id="chief_of_staff",
            human_owner_id=principal.user_id,
            title=(goal[:157] + "...") if len(goal) > 160 else goal,
            status="active",
            character_version_id=clara.default_version_id,
            model_policy_id=clara_policy.id,
            mission_id=mission.id,
        ))
        for employee_id in participant_ids:
            mission_role = (
                "orchestrator" if employee_id == "chief_of_staff"
                else "accountable" if employee_id == accountable_employee_id
                else "specialist"
            )
            self.store.save_mission_participant(AiMissionParticipant(
                id=f"aimp_{uuid4().hex}",
                **scope,
                mission_id=mission.id,
                employee_id=employee_id,
                mission_role=mission_role,
                character_version_id=profiles[employee_id].default_version_id,
                model_policy_id=policies[employee_id].id,
                status="active",
            ))
        return mission, conversation

    def run_mission(self, *, principal, mission_id: str):
        account_id, space_id = _principal_scope(principal)
        scope = {
            "tenant_id": principal.tenant_id,
            "account_id": account_id,
            "space_id": space_id,
        }
        mission = self.store.get_mission(mission_id, **scope)
        if not mission:
            raise KeyError(f"AI employee mission not found: {mission_id}")
        if mission.sponsor_id != principal.user_id:
            raise PermissionError("Only the human mission sponsor can run this mission.")
        if mission.status in {"completed", "cancelled"}:
            yield {"type": "mission_done", "mission_id": mission.id, "status": mission.status}
            return
        participants = self.store.list_mission_participants(mission.id, **scope)
        validate_mission_squad(tuple(row.employee_id for row in participants))
        conversation = next((row for row in self.store.list_conversations(
            **scope, human_owner_id=principal.user_id,
        ) if row.mission_id == mission.id), None)
        if not conversation:
            raise ValueError("AI employee mission conversation is missing.")
        hits = tuple(self.retrieval_service.retrieve(principal, mission.goal))
        started = time.monotonic()
        incomplete: list[str] = []
        mission = self.store.save_mission(replace(mission, status="running", phase="scope", error=""))
        yield {"type": "mission", "mission_id": mission.id, "status": mission.status, "phase": mission.phase}

        try:
            scope_result = self._turn(
                mission=mission,
                conversation=conversation,
                participant=self._participant(participants, "chief_of_staff"),
                phase="scope",
                instruction=(
                    "Scope the mission: clarify the decision, success criteria, constraints, required evidence, "
                    "accountable executive, and how the squad should divide the work."
                ),
                context=(),
                hits=hits,
                scope=scope,
            )
        except MissionTurnError:
            paused = self.store.save_mission(replace(
                mission, status="paused", phase="scope", error="Chief of Staff scope turn failed.",
            ))
            yield {"type": "mission_paused", "mission_id": paused.id, "phase": paused.phase, "reason": paused.error}
            return
        yield self._turn_event("scope", "chief_of_staff", scope_result)
        if self._budget_exhausted(mission, started, scope):
            yield self._pause_for_budget(mission, "scope")
            return

        position_results: list[tuple[str, MissionAgentResult]] = []
        failed_employee_ids: set[str] = set()
        mission = self.store.save_mission(replace(mission, phase="positions"))
        for participant in participants:
            if participant.employee_id == "chief_of_staff":
                continue
            try:
                result = self._turn(
                    mission=mission,
                    conversation=conversation,
                    participant=participant,
                    phase="position",
                    instruction=(
                        "Give your independent domain position. State evidence, assumptions, recommendation, "
                        "risks, and what you disagree with or still need."
                    ),
                    context=(self._context("chief_of_staff", "scope", scope_result.content),),
                    hits=hits,
                    scope=scope,
                )
                position_results.append((participant.employee_id, result))
                yield self._turn_event("position", participant.employee_id, result)
            except MissionTurnError:
                incomplete.append(f"{participant.employee_id}:position")
                failed_employee_ids.add(participant.employee_id)
                self.store.save_mission_participant(replace(participant, status="failed"))
                yield {"type": "participant_failed", "employee_id": participant.employee_id, "phase": "position"}
            if self._budget_exhausted(mission, started, scope):
                yield self._pause_for_budget(mission, "positions")
                return

        challenge_context = (
            self._context("chief_of_staff", "scope", scope_result.content),
            *(self._context(employee_id, "position", result.content) for employee_id, result in position_results),
        )
        challenge_results: list[tuple[str, MissionAgentResult]] = []
        mission = self.store.save_mission(replace(mission, phase="challenge"))
        for participant in participants:
            if participant.employee_id == "chief_of_staff" or participant.employee_id in failed_employee_ids:
                continue
            try:
                result = self._turn(
                    mission=mission,
                    conversation=conversation,
                    participant=participant,
                    phase="challenge",
                    instruction=(
                        "Run one constructive challenge round. Identify the most important weak assumption, "
                        "conflict, dependency, or missing control in the other positions, then propose a resolution."
                    ),
                    context=challenge_context,
                    hits=hits,
                    scope=scope,
                )
                challenge_results.append((participant.employee_id, result))
                yield self._turn_event("challenge", participant.employee_id, result)
            except MissionTurnError:
                incomplete.append(f"{participant.employee_id}:challenge")
                yield {"type": "participant_failed", "employee_id": participant.employee_id, "phase": "challenge"}
            if self._budget_exhausted(mission, started, scope):
                yield self._pause_for_budget(mission, "challenge")
                return

        all_context = (
            *challenge_context,
            *(self._context(employee_id, "challenge", result.content) for employee_id, result in challenge_results),
        )
        accountable = self._participant(participants, mission.accountable_employee_id)
        mission = self.store.save_mission(replace(mission, phase="accountable_plan"))
        try:
            plan_result = self._turn(
                mission=mission,
                conversation=conversation,
                participant=accountable,
                phase="accountable_plan",
                instruction=(
                    "Produce the accountable domain plan: decision, milestones, owners, dependencies, controls, "
                    "measures, unresolved risks, and any actions that require human approval."
                ),
                context=all_context,
                hits=hits,
                scope=scope,
            )
        except MissionTurnError:
            paused = self.store.save_mission(replace(
                mission, status="paused", error="Accountable executive plan turn failed.",
            ))
            yield {"type": "mission_paused", "mission_id": paused.id, "phase": paused.phase, "reason": paused.error}
            return
        yield self._turn_event("accountable_plan", accountable.employee_id, plan_result)
        if self._budget_exhausted(mission, started, scope):
            yield self._pause_for_budget(mission, "accountable_plan")
            return

        synthesis_context = (*all_context, self._context(
            accountable.employee_id, "accountable_plan", plan_result.content,
        ))
        mission = self.store.save_mission(replace(mission, phase="synthesis"))
        try:
            synthesis = self._turn(
                mission=mission,
                conversation=conversation,
                participant=self._participant(participants, "chief_of_staff"),
                phase="synthesis",
                instruction=(
                    "Synthesize the final mission brief. Preserve material dissent and missing input. "
                    "Separate decisions, assumptions, dependencies, risks, owners, next actions, and human approvals."
                ),
                context=synthesis_context,
                hits=hits,
                scope=scope,
            )
        except MissionTurnError:
            paused = self.store.save_mission(replace(
                mission, status="paused", error="Chief of Staff synthesis turn failed.",
            ))
            yield {"type": "mission_paused", "mission_id": paused.id, "phase": paused.phase, "reason": paused.error}
            return
        synthesis_message = self._message_for_run(conversation.id, synthesis, scope)
        completed = self.store.save_mission(replace(
            mission,
            status="completed",
            phase="human_review",
            synthesis_message_id=synthesis_message.id if synthesis_message else "",
            error=("Incomplete specialist input: " + ", ".join(incomplete)) if incomplete else "",
        ))
        yield self._turn_event("synthesis", "chief_of_staff", synthesis)
        yield {
            "type": "mission_done",
            "mission_id": completed.id,
            "status": completed.status,
            "phase": completed.phase,
            "incomplete": bool(incomplete),
            "missing_inputs": incomplete,
        }

    def cancel_mission(self, *, principal, mission_id: str) -> AiMission:
        account_id, space_id = _principal_scope(principal)
        scope = {"tenant_id": principal.tenant_id, "account_id": account_id, "space_id": space_id}
        mission = self.store.get_mission(mission_id, **scope)
        if not mission or mission.sponsor_id != principal.user_id:
            raise KeyError(f"AI employee mission not found: {mission_id}")
        if mission.status == "completed":
            raise ValueError("Completed AI employee missions cannot be cancelled.")
        return self.store.save_mission(replace(mission, status="cancelled", phase="cancelled"))

    def _turn(
        self,
        *,
        mission,
        conversation,
        participant,
        phase,
        instruction,
        context,
        hits,
        scope,
    ) -> MissionAgentResult:
        idempotency_key = f"mission:{mission.id}:{phase}:{participant.employee_id}"
        input_hash = hashlib.sha256(json.dumps({
            "mission_id": mission.id,
            "phase": phase,
            "employee_id": participant.employee_id,
            "instruction": instruction,
            "context": context,
            "character_version_id": participant.character_version_id,
            "model_policy_id": participant.model_policy_id,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = self.store.get_run_by_idempotency(idempotency_key, **scope)
        if existing and existing.input_hash != input_hash:
            raise MissionTurnError("Mission turn idempotency conflict.")
        if existing and existing.status == "completed":
            message = next((row for row in self.store.list_messages(conversation.id, **scope)
                            if row.run_id == existing.id and row.speaker_type == "employee"), None)
            if message:
                return MissionAgentResult(
                    content=message.content,
                    citations=message.citations,
                    prompt_tokens=existing.prompt_tokens,
                    completion_tokens=existing.completion_tokens,
                    cost_usd=existing.cost_usd,
                    provider_session_ref=existing.provider_session_ref,
                    backend=existing.backend,
                    model=existing.model,
                    warning=existing.warning,
                )
        version = self.store.get_character_version(participant.character_version_id, **scope)
        policy = self.store.get_model_policy(participant.employee_id, **scope)
        if not version or not policy or policy.id != participant.model_policy_id:
            raise MissionTurnError("Pinned AI employee configuration is unavailable.")
        memories = tuple(active_approved_memories(self.store.list_memories(
            **scope, employee_id=participant.employee_id, status="approved",
        )))
        run = self.store.save_run(AiAgentRun(
            id=existing.id if existing else f"air_{uuid4().hex}",
            **scope,
            conversation_id=conversation.id,
            mission_id=mission.id,
            employee_id=participant.employee_id,
            backend=existing.backend if existing else policy.provider,
            model=existing.model if existing else policy.model,
            idempotency_key=idempotency_key,
            status="running",
            input_hash=input_hash,
            started_at=now_iso(),
        ))
        try:
            result = self.executor.generate(MissionAgentRequest(
                mission=mission,
                employee_id=participant.employee_id,
                phase=phase,
                instruction=instruction,
                context=tuple(context),
                character_payload=version.payload,
                policy=policy,
                memories=memories,
                hits=tuple(hits),
            ))
            if not result.content.strip():
                raise MissionTurnError("AI employee mission turn returned no content.")
        except Exception as exc:
            self.store.save_run(replace(
                run, status="failed", error="AI employee mission turn failed.", completed_at=now_iso(),
            ))
            raise MissionTurnError("AI employee mission turn failed.") from exc
        self.store.save_message(AiEmployeeMessage(
            id=f"aim_{uuid4().hex}",
            **scope,
            conversation_id=conversation.id,
            speaker_type="employee",
            speaker_id=participant.employee_id,
            visibility="shared",
            content=result.content,
            citations=result.citations,
            run_id=run.id,
        ))
        self.store.save_run(replace(
            run,
            status="completed",
            backend=result.backend or run.backend,
            model=result.model or run.model,
            provider_session_ref=result.provider_session_ref,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd,
            warning=result.warning,
            error="",
            completed_at=now_iso(),
        ))
        return result

    def _budget_exhausted(self, mission, started: float, scope: dict) -> bool:
        runs = [row for row in self.store.list_runs(**scope)
                if row.mission_id == mission.id and row.status == "completed"]
        tokens = sum(row.prompt_tokens + row.completion_tokens for row in runs)
        cost = sum(row.cost_usd for row in runs)
        elapsed = time.monotonic() - started
        return (
            tokens >= mission.token_budget
            or cost >= mission.cost_budget_usd
            or elapsed >= mission.time_budget_seconds
        )

    def _pause_for_budget(self, mission, phase: str) -> dict:
        paused = self.store.save_mission(replace(
            mission, status="paused", phase=phase, error="Mission budget exhausted.",
        ))
        return {
            "type": "mission_paused",
            "mission_id": paused.id,
            "phase": phase,
            "reason": paused.error,
        }

    def _message_for_run(self, conversation_id: str, result: MissionAgentResult, scope: dict):
        messages = self.store.list_messages(conversation_id, **scope)
        return next((row for row in reversed(messages)
                     if row.speaker_id == "chief_of_staff" and row.content == result.content), None)

    @staticmethod
    def _participant(participants, employee_id: str):
        row = next((participant for participant in participants if participant.employee_id == employee_id), None)
        if not row:
            raise MissionTurnError(f"Required mission participant is missing: {employee_id}")
        return row

    @staticmethod
    def _context(employee_id: str, phase: str, content: str) -> dict[str, str]:
        return {"speaker": employee_id, "content": f"[{phase}] {content}"}

    @staticmethod
    def _turn_event(phase: str, employee_id: str, result: MissionAgentResult) -> dict:
        return {
            "type": "agent_turn",
            "phase": phase,
            "employee_id": employee_id,
            "content": result.content,
            "citations": list(result.citations),
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cost_usd": result.cost_usd,
            },
        }


def _max_classification(hits) -> Classification:
    highest = Classification.PUBLIC
    for hit in hits:
        highest = max(highest, Classification.parse(
            hit.chunk.meta.get("classification", Classification.RESTRICTED),
        ))
    return highest


def _principal_scope(principal) -> tuple[str, str]:
    if (
        principal.principal_type != "human"
        or not principal.account_id
        or not principal.space_ids
        or len(principal.space_ids) != 1
        or principal.account_id != principal.tenant_id
    ):
        raise PermissionError("AI employee missions require one explicit human account and space.")
    return principal.account_id, next(iter(principal.space_ids))

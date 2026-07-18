"""Operational records and store contract for the AI Employees module."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from app.ai_employees.contracts import AiEmployee, assert_no_raw_secrets


CHARACTER_VERSION_STATES = frozenset({"draft", "published"})
PROFILE_STATUSES = frozenset({"active", "paused"})
CONVERSATION_STATUSES = frozenset({"active", "archived"})
MISSION_STATUSES = frozenset({"draft", "queued", "running", "paused", "completed", "cancelled", "failed"})
RUN_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled", "blocked"})
RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "blocked"})
MEMORY_STATUSES = frozenset({"pending", "approved", "rejected", "deleted"})
CONNECTOR_STATUSES = frozenset({"active", "paused", "revoked", "error"})

# A lease expiry is deliberately terminal for an idempotency key. Replaying a
# paid provider request after losing ownership can duplicate spend and produce
# two competing answers for the same human turn.
AI_AGENT_RUN_LEASE_EXPIRED_ERROR = "AI employee turn lease expired before completion."


@dataclass(frozen=True)
class AiEmployeeProfile:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    employee_id: str
    role: str
    department: str
    pod: str
    reports_to: str
    status: str
    default_version_id: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AiEmployeeCharacterVersion:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    employee_id: str
    version: int
    state: str
    payload: dict
    checksum: str
    author_id: str
    base_version_id: str = ""
    created_at: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class AiEmployeeModelPolicy:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    employee_id: str
    version: int
    provider: str
    model: str
    task_overrides: dict
    allowed_fallbacks: tuple[str, ...]
    data_ceiling: str
    cost_limit_usd: float
    status: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AiEmployeeConversation:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    employee_id: str
    human_owner_id: str
    title: str
    status: str
    character_version_id: str
    model_policy_id: str
    mission_id: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AiEmployeeMessage:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    conversation_id: str
    speaker_type: str
    speaker_id: str
    visibility: str
    content: str
    citations: tuple[str, ...] = ()
    run_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class AiMission:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    goal: str
    sponsor_id: str
    accountable_employee_id: str
    status: str
    phase: str
    token_budget: int
    time_budget_seconds: int
    cost_budget_usd: float
    synthesis_message_id: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AiMissionParticipant:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    mission_id: str
    employee_id: str
    mission_role: str
    character_version_id: str
    model_policy_id: str
    status: str
    joined_at: str = ""


@dataclass(frozen=True)
class AiAgentRun:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    conversation_id: str
    mission_id: str
    employee_id: str
    backend: str
    model: str
    idempotency_key: str
    status: str
    input_hash: str
    provider_session_ref: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    warning: str = ""
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    # Lease fields are internal coordination capabilities. They must never be
    # included in customer-facing run payloads or privacy exports.
    lease_token: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""


@dataclass(frozen=True)
class AiAgentRunClaim:
    """Result of atomically starting or looking up an idempotent AI turn."""

    run: AiAgentRun
    acquired: bool


@dataclass(frozen=True)
class AiEmployeeMemory:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    employee_id: str
    content: str
    source_refs: tuple[str, ...]
    classification: str
    status: str
    retention_until: str
    author_id: str
    approved_by: str = ""
    created_at: str = ""
    approved_at: str = ""


@dataclass(frozen=True)
class AiConnectorBinding:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    provider: str
    credential_ref: str
    resource_type: str
    resource_ids: tuple[str, ...]
    employee_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    status: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AiActionProposalRecord:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    mission_id: str
    conversation_id: str
    run_id: str
    employee_id: str
    action_type: str
    target_system: str
    risk_level: str
    classification: str
    actionability: str
    source_record_ids: tuple[str, ...]
    payload_summary: str
    payload: dict
    payload_hash: str
    required_approver_role: str
    expires_at: str
    idempotency_key: str
    status: str
    requires_approval: bool
    reason: str
    approved_by: str = ""
    approved_at: str = ""
    execution_ref: str = ""
    created_at: str = ""
    updated_at: str = ""


class AiEmployeeStore(Protocol):
    def seed_defaults(
        self, *, tenant_id: str, account_id: str, space_id: str, author_id: str,
        default_model: str = "gemini/gemini-2.5-flash",
    ) -> list[AiEmployeeProfile]: ...

    def list_profiles(self, *, tenant_id: str, account_id: str, space_id: str) -> list[AiEmployeeProfile]: ...

    def get_profile(
        self, employee_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeProfile]: ...

    def begin_or_get_run(
        self,
        run: AiAgentRun,
        *,
        human_message: Optional[AiEmployeeMessage] = None,
    ) -> AiAgentRunClaim: ...

    def heartbeat_run(
        self,
        run_id: str,
        *,
        tenant_id: str,
        account_id: str,
        space_id: str,
        lease_token: str,
        lease_expires_at: str,
    ) -> Optional[AiAgentRun]: ...

    def finalize_owned_run(
        self,
        run: AiAgentRun,
        *,
        lease_token: str,
        assistant_message: Optional[AiEmployeeMessage] = None,
    ) -> Optional[AiAgentRun]: ...

    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict: ...

    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def message_sort_key(message: AiEmployeeMessage) -> tuple[str, str, int, str]:
    """Return a stable conversation order without relying on random message IDs.

    A direct turn creates its human and employee messages independently. On
    hosts with coarse clock resolution they can receive the same timestamp, so
    sorting by UUID would occasionally show the answer before the question.
    Keep the two messages from a run together and use their defined turn order
    as the tie-breaker.
    """
    speaker_order = {
        "human": 0,
        "employee": 1,
        "system": 2,
        "tool": 3,
    }.get(message.speaker_type, 9)
    return (message.created_at, message.run_id or message.id, speaker_order, message.id)


def mission_participant_sort_key(participant: AiMissionParticipant) -> tuple[int, str, str, str]:
    """Return the deterministic execution order for a mission squad.

    Membership is set by mission semantics, not by UUID creation order: the
    Chief of Staff scopes and synthesizes first, the accountable executive
    follows, then specialist positions run in a stable employee order.
    """
    mission_role_order = {
        "orchestrator": 0,
        "accountable": 1,
        "specialist": 2,
    }
    return (
        mission_role_order.get(participant.mission_role, 9),
        participant.employee_id,
        participant.joined_at,
        participant.id,
    )


def validate_scope(*, tenant_id: str, account_id: str, space_id: str) -> None:
    if not (tenant_id or "").strip() or not (account_id or "").strip() or not (space_id or "").strip():
        raise ValueError("tenant_id, account_id, and space_id are required.")


def scope_matches(record, *, tenant_id: str, account_id: str, space_id: str = "") -> bool:
    if record.tenant_id != tenant_id or record.account_id != account_id:
        return False
    return not space_id or record.space_id == space_id


def stable_record_id(prefix: str, *parts: str) -> str:
    raw = ":".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def character_checksum(payload: dict) -> str:
    assert_no_raw_secrets(payload, "character")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_character_payload(employee: AiEmployee) -> dict:
    return {
        "display_name": employee.name,
        "fictional_age": employee.age,
        "country": employee.country,
        "pronouns": employee.pronouns,
        "biography": employee.prompt_safe_description,
        "avatar_url": "",
        "personality": list(employee.personality),
        "tone": employee.tone,
        "vocabulary": "Plain language appropriate to the role and audience.",
        "communication_style": employee.tone,
        "strengths": list(employee.strengths),
        "watch_outs": list(employee.watch_outs),
        "working_style": employee.working_style,
        "collaboration_behavior": "Challenge constructively, cite evidence, and preserve material dissent.",
        "role_focus": employee.prompt_safe_description,
        "character_prompt": employee.character_prompt,
        "examples": [],
    }


def public_record_dict(record) -> dict:
    """Return a JSON-ready copy of a module record."""

    return asdict(record)

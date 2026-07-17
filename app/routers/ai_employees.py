"""Human-facing API for the optional AI Employees module."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.ai_employees.access import (
    authorize_ai_employee_purpose,
    authorize_ai_employee_reader,
    find_ai_employee_installation,
)
from app.ai_employees.actions import WORK_PRODUCT_RECORD_TYPES
from app.ai_employees.base import AiEmployeeProfile
from app.ai_employees.characters import (
    character_preview,
    create_character_draft,
    publish_character_version,
    reset_character,
    rollback_character,
)
from app.ai_employees.contracts import (
    AI_EMPLOYEE_PODS,
    AI_EMPLOYEES_CONTRACT_VERSION,
    LEADERSHIP_COUNCIL_IDS,
    MAX_MISSION_SQUAD_SIZE,
    get_ai_employee,
)
from app.auth.account_access import authorize_account_admin, is_account_admin, is_account_member
from app.auth.principal import Principal, resolve_principal
from app.deps import (
    get_ai_employee_action_service,
    get_ai_employee_backend_registry,
    get_ai_employee_google_calendar_connector,
    get_ai_employee_mission_service,
    get_ai_employee_runtime,
    get_ai_employee_store,
    get_intake_store,
    get_platform_store,
)
from app.platform.base import AuditEvent
from app.security.policy import Classification
from app.ai_employees.memory_service import (
    create_memory_candidate,
    decide_memory,
    delete_memory,
)


router = APIRouter(prefix="/api/ai-employees", tags=["ai-employees"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AiEmployeeWorkspaceOut(StrictModel):
    account_id: str
    account_name: str
    space_id: str
    space_name: str
    space_kind: str
    installation_status: str
    can_configure: bool
    can_run_missions: bool
    can_manage_connectors: bool


class AiEmployeeOut(StrictModel):
    profile_id: str
    employee_id: str
    name: str
    fictional_age: int
    country: str
    pronouns: str
    role: str
    department: str
    pod: str
    reports_to: str
    status: str
    leadership_council: bool
    personality: list[str]
    tone: str
    strengths: list[str]
    watch_outs: list[str]
    working_style: str
    biography: str
    avatar_url: str
    character_version_id: str
    character_version: int
    model_policy_id: str
    model_provider: str
    model: str
    default_mode: str
    safe_actions: list[str]
    approval_rule: str
    productivity_metrics: list[str]
    never_without_approval: list[str]


class AiEmployeeTeamOut(StrictModel):
    account_id: str
    space_id: str
    installation_status: str
    contract_version: str
    max_mission_squad_size: int
    leadership_council_ids: list[str]
    pods: dict[str, list[str]]
    can_configure: bool
    can_run_missions: bool
    can_manage_connectors: bool
    agents: list[AiEmployeeOut]


class AiEmployeeStatusUpdate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    status: Literal["active", "paused"]


class AiEmployeeConversationCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    employee_id: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=160)


class AiEmployeeConversationOut(StrictModel):
    id: str
    account_id: str
    space_id: str
    employee_id: str
    human_owner_id: str
    title: str
    status: str
    character_version_id: str
    model_policy_id: str
    mission_id: str
    created_at: str
    updated_at: str


class AiEmployeeMessageOut(StrictModel):
    id: str
    conversation_id: str
    speaker_type: str
    speaker_id: str
    visibility: str
    content: str
    citations: list[str]
    run_id: str
    created_at: str


class AiEmployeeTurnCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class AiEmployeeModelHealthOut(StrictModel):
    provider: str
    available: bool
    reason: str


class AiEmployeeModelPostureOut(StrictModel):
    employee_id: str
    provider: str
    model: str
    data_ceiling: str
    cost_limit_usd: float
    status: str


class AiEmployeeModelsOut(StrictModel):
    health: list[AiEmployeeModelHealthOut]
    policies: list[AiEmployeeModelPostureOut]


class AiEmployeeCharacterPatch(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    fictional_age: Optional[int] = Field(default=None, ge=18, le=80)
    country: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pronouns: Optional[str] = Field(default=None, min_length=1, max_length=80)
    biography: Optional[str] = Field(default=None, max_length=2_000)
    avatar_url: Optional[str] = Field(default=None, max_length=1_000)
    personality: Optional[list[str]] = Field(default=None, max_length=20)
    tone: Optional[str] = Field(default=None, max_length=1_000)
    vocabulary: Optional[str] = Field(default=None, max_length=2_000)
    communication_style: Optional[str] = Field(default=None, max_length=2_000)
    strengths: Optional[list[str]] = Field(default=None, max_length=20)
    watch_outs: Optional[list[str]] = Field(default=None, max_length=20)
    working_style: Optional[str] = Field(default=None, max_length=3_000)
    collaboration_behavior: Optional[str] = Field(default=None, max_length=3_000)
    role_focus: Optional[str] = Field(default=None, max_length=3_000)
    character_prompt: Optional[str] = Field(default=None, max_length=12_000)
    examples: Optional[list[str]] = Field(default=None, max_length=20)


class AiEmployeeCharacterVersionOut(StrictModel):
    id: str
    employee_id: str
    version: int
    state: str
    payload: dict
    checksum: str
    author_id: str
    base_version_id: str
    created_at: str
    published_at: str
    preview: str


class AiEmployeeCharacterPublish(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    expected_profile_version_id: str = Field(min_length=1, max_length=160)


class AiEmployeeCharacterRollback(AiEmployeeCharacterPublish):
    source_version_id: str = Field(min_length=1, max_length=160)


class AiEmployeeMemoryCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    employee_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4_000)
    source_refs: list[str] = Field(min_length=1, max_length=50)
    classification: Literal["public", "internal", "confidential", "restricted"]
    retention_until: str = Field(min_length=1, max_length=80)


class AiEmployeeMemoryDecision(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    decision: Literal["approved", "rejected"]


class AiEmployeeMemoryOut(StrictModel):
    id: str
    employee_id: str
    content: str
    source_refs: list[str]
    classification: str
    status: str
    retention_until: str
    author_id: str
    approved_by: str
    created_at: str
    approved_at: str


class AiMissionCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=4_000)
    accountable_employee_id: str = Field(min_length=1, max_length=120)
    participant_ids: list[str] = Field(min_length=2, max_length=6)
    token_budget: int = Field(default=30_000, ge=1_000, le=500_000)
    time_budget_seconds: int = Field(default=900, ge=30, le=86_400)
    cost_budget_usd: float = Field(default=10.0, gt=0, le=1_000)


class AiMissionRunRequest(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)


class AiMissionParticipantOut(StrictModel):
    employee_id: str
    mission_role: str
    character_version_id: str
    model_policy_id: str
    status: str


class AiMissionUsageOut(StrictModel):
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class AiMissionOut(StrictModel):
    id: str
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
    synthesis_message_id: str
    error: str
    created_at: str
    updated_at: str
    participants: list[AiMissionParticipantOut]
    usage: AiMissionUsageOut


class AiMissionDetailOut(AiMissionOut):
    conversation_id: str
    messages: list[AiEmployeeMessageOut]


class AiEmployeeWorkProductCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    employee_id: str = Field(min_length=1, max_length=120)
    record_type: Literal["brief", "task", "policy", "note", "document", "action"]
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=50_000)
    classification: Literal["public", "internal", "confidential", "restricted"]
    source_record_ids: list[str] = Field(min_length=1, max_length=100)
    mission_id: str = Field(default="", max_length=160)
    conversation_id: str = Field(default="", max_length=160)


class AiEmployeeWorkProductOut(StrictModel):
    id: str
    employee_id: str
    record_type: str
    title: str
    content: str
    classification: str
    source_record_ids: list[str]
    mission_id: str
    conversation_id: str
    created_at: str


class AiEmployeeActionCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    employee_id: str = Field(min_length=1, max_length=120)
    action_type: str = Field(min_length=1, max_length=160)
    target_system: str = Field(min_length=1, max_length=160)
    risk_level: Literal["low", "medium", "high", "critical"]
    classification: Literal["public", "internal", "confidential", "restricted"]
    actionability: Literal["answer_only", "draft_only", "approval_required", "automation_allowed"]
    source_record_ids: list[str] = Field(min_length=1, max_length=100)
    payload_summary: str = Field(min_length=1, max_length=1_000)
    payload: dict
    idempotency_key: str = Field(min_length=1, max_length=240)
    expires_at: str = Field(default="", max_length=80)
    mission_id: str = Field(default="", max_length=160)
    conversation_id: str = Field(default="", max_length=160)
    run_id: str = Field(default="", max_length=160)


class AiEmployeeActionDecision(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    decision: Literal["approved", "rejected", "changes_requested", "duplicate"]
    note: str = Field(default="", max_length=1_000)


class AiEmployeeActionScope(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)


class AiEmployeeActionOut(StrictModel):
    id: str
    mission_id: str
    conversation_id: str
    run_id: str
    employee_id: str
    action_type: str
    target_system: str
    risk_level: str
    classification: str
    actionability: str
    source_record_ids: list[str]
    payload_summary: str
    payload: dict
    payload_hash: str
    required_approver_role: str
    expires_at: str
    idempotency_key: str
    status: str
    requires_approval: bool
    reason: str
    approved_by: str
    approved_at: str
    execution_ref: str
    created_at: str
    updated_at: str


class AiConnectorHealthOut(StrictModel):
    provider: str
    available: bool
    reason: str
    scopes: list[str]


class AiConnectorBindingOut(StrictModel):
    id: str
    provider: str
    resource_type: str
    resource_ids: list[str]
    employee_ids: list[str]
    capabilities: list[str]
    status: str
    created_at: str
    updated_at: str


class GoogleCalendarOAuthStart(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    employee_ids: list[str] = Field(min_length=1, max_length=16)
    capabilities: list[str] = Field(min_length=1, max_length=10)
    resource_ids: list[str] = Field(
        default_factory=lambda: ["primary"], min_length=1, max_length=100,
    )


class GoogleCalendarOAuthStartOut(StrictModel):
    authorization_url: str
    state_expires_at: int


class GoogleCalendarOAuthCallback(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=8_000)
    code: str = Field(min_length=1, max_length=8_000)


class GoogleCalendarBindingUpdate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    employee_ids: list[str] = Field(min_length=1, max_length=16)
    capabilities: list[str] = Field(min_length=1, max_length=10)
    resource_ids: list[str] = Field(min_length=1, max_length=100)


class GoogleCalendarOut(StrictModel):
    id: str
    summary: str
    primary: bool
    access_role: str


@router.get("/workspaces", response_model=list[AiEmployeeWorkspaceOut])
def list_ai_employee_workspaces(principal: Principal = Depends(resolve_principal)):
    if principal.principal_type != "human":
        raise HTTPException(status_code=403, detail="Human session required.")
    platform = get_platform_store()
    account = platform.get_account(principal.tenant_id)
    if not account:
        return []
    admin = is_account_admin(principal, account, platform)
    rows: list[AiEmployeeWorkspaceOut] = []
    for space in platform.list_spaces(account.id):
        if space.status != "active" or not is_account_member(principal, account, space.id, platform):
            continue
        installation = find_ai_employee_installation(account.id, space.id, platform)
        if not installation or "ai_employee_read" not in installation.allowed_purposes:
            continue
        active = installation.status == "active"
        rows.append(AiEmployeeWorkspaceOut(
            account_id=account.id,
            account_name=account.name,
            space_id=space.id,
            space_name=space.name,
            space_kind=space.kind,
            installation_status=installation.status,
            can_configure=admin and active and "ai_employee_configure" in installation.allowed_purposes,
            can_run_missions=active and "ai_employee_mission_run" in installation.allowed_purposes,
            can_manage_connectors=(
                admin and active and "ai_employee_connector_manage" in installation.allowed_purposes
            ),
        ))
    return rows


@router.get("/team", response_model=AiEmployeeTeamOut)
def get_ai_employee_team(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    platform = get_platform_store()
    access = authorize_ai_employee_reader(principal, account_id, space_id, platform)
    store = get_ai_employee_store()
    profiles = store.list_profiles(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    if access.active and not profiles:
        profiles = store.seed_defaults(
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            author_id=f"system:{AI_EMPLOYEES_CONTRACT_VERSION}",
        )
        _record_audit(
            principal,
            action="ai_employees.defaults_seeded",
            account_id=account_id,
            space_id=space_id,
            target_id=access.installation.id,
            purpose="ai_employee_configure",
            meta={"contract_version": AI_EMPLOYEES_CONTRACT_VERSION, "employee_count": len(profiles)},
        )
    active = access.active
    return AiEmployeeTeamOut(
        account_id=account_id,
        space_id=space_id,
        installation_status=access.installation.status,
        contract_version=AI_EMPLOYEES_CONTRACT_VERSION,
        max_mission_squad_size=MAX_MISSION_SQUAD_SIZE,
        leadership_council_ids=list(LEADERSHIP_COUNCIL_IDS),
        pods={name: list(employee_ids) for name, employee_ids in AI_EMPLOYEE_PODS.items()},
        can_configure=(
            access.is_admin and active and "ai_employee_configure" in access.installation.allowed_purposes
        ),
        can_run_missions=active and "ai_employee_mission_run" in access.installation.allowed_purposes,
        can_manage_connectors=(
            access.is_admin and active
            and "ai_employee_connector_manage" in access.installation.allowed_purposes
        ),
        agents=[_agent_out(profile, store) for profile in profiles],
    )


@router.get("/agents/{employee_id}", response_model=AiEmployeeOut)
def get_ai_employee_detail(
    employee_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    store = get_ai_employee_store()
    profile = store.get_profile(
        employee_id, tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    if not profile:
        raise HTTPException(status_code=404, detail="AI employee not found.")
    return _agent_out(profile, store)


@router.patch("/agents/{employee_id}/status", response_model=AiEmployeeOut)
def set_ai_employee_status(
    employee_id: str,
    body: AiEmployeeStatusUpdate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal,
        body.account_id,
        body.space_id,
        "ai_employee_configure",
        get_platform_store(),
        admin_required=True,
    )
    store = get_ai_employee_store()
    profile = store.get_profile(
        employee_id,
        tenant_id=principal.tenant_id,
        account_id=body.account_id,
        space_id=body.space_id,
    )
    if not profile:
        raise HTTPException(status_code=404, detail="AI employee not found.")
    saved = store.save_profile(replace(profile, status=body.status))
    _record_audit(
        principal,
        action="ai_employee.status_changed",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=saved.id,
        purpose="ai_employee_configure",
        meta={"employee_id": employee_id, "status": saved.status},
    )
    return _agent_out(saved, store)


@router.post("/conversations", response_model=AiEmployeeConversationOut)
def create_ai_employee_conversation(
    body: AiEmployeeConversationCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal,
        body.account_id,
        body.space_id,
        "ai_employee_mission_run",
        get_platform_store(),
    )
    _ensure_seeded(principal, body.account_id, body.space_id)
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        conversation = get_ai_employee_runtime().create_conversation(
            principal=scoped,
            account_id=body.account_id,
            space_id=body.space_id,
            employee_id=body.employee_id,
            title=body.title,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    return _conversation_out(conversation)


@router.get("/conversations", response_model=list[AiEmployeeConversationOut])
def list_ai_employee_conversations(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    rows = get_ai_employee_store().list_conversations(
        tenant_id=principal.tenant_id,
        account_id=account_id,
        space_id=space_id,
        human_owner_id=principal.user_id,
    )
    return [_conversation_out(row) for row in rows]


@router.get("/conversations/{conversation_id}/messages", response_model=list[AiEmployeeMessageOut])
def list_ai_employee_messages(
    conversation_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    store = get_ai_employee_store()
    conversation = store.get_conversation(
        conversation_id,
        tenant_id=principal.tenant_id,
        account_id=account_id,
        space_id=space_id,
    )
    if not conversation or conversation.human_owner_id != principal.user_id:
        raise HTTPException(status_code=404, detail="AI employee conversation not found.")
    return [
        _message_out(row)
        for row in store.list_messages(
            conversation_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
    ]


@router.post("/conversations/{conversation_id}/turns")
def stream_ai_employee_turn(
    conversation_id: str,
    body: AiEmployeeTurnCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal,
        body.account_id,
        body.space_id,
        "ai_employee_mission_run",
        get_platform_store(),
    )
    scoped = _runtime_principal(principal, body.account_id, body.space_id)

    def event_stream():
        turn_events = None
        try:
            turn_events = get_ai_employee_runtime().stream_turn(
                principal=scoped,
                conversation_id=conversation_id,
                question=body.question,
                idempotency_key=body.idempotency_key,
            )
            for event in turn_events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            message, code = _runtime_error_payload(exc)
            yield f"data: {json.dumps({'type': 'error', 'code': code, 'message': message})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'replayed': False})}\n\n"
        finally:
            if turn_events is not None:
                turn_events.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/models", response_model=AiEmployeeModelsOut)
def get_ai_employee_models(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    policies = get_ai_employee_store().list_model_policies(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    return AiEmployeeModelsOut(
        health=[AiEmployeeModelHealthOut(**row) for row in get_ai_employee_backend_registry().health()],
        policies=[AiEmployeeModelPostureOut(
            employee_id=row.employee_id,
            provider=row.provider,
            model=row.model,
            data_ceiling=row.data_ceiling,
            cost_limit_usd=row.cost_limit_usd,
            status=row.status,
        ) for row in policies],
    )


@router.get(
    "/agents/{employee_id}/character/versions",
    response_model=list[AiEmployeeCharacterVersionOut],
)
def list_ai_employee_character_versions(
    employee_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    authorize_account_admin(principal, account_id, get_platform_store())
    rows = get_ai_employee_store().list_character_versions(
        tenant_id=principal.tenant_id,
        account_id=account_id,
        space_id=space_id,
        employee_id=employee_id,
    )
    return [_character_version_out(row) for row in rows]


@router.post(
    "/agents/{employee_id}/character/drafts",
    response_model=AiEmployeeCharacterVersionOut,
)
def create_ai_employee_character_draft(
    employee_id: str,
    body: AiEmployeeCharacterPatch,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal,
        body.account_id,
        body.space_id,
        "ai_employee_configure",
        get_platform_store(),
        admin_required=True,
    )
    patch = body.model_dump(exclude={"account_id", "space_id"}, exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="At least one editable character field is required.")
    try:
        version = create_character_draft(
            get_ai_employee_store(),
            tenant_id=principal.tenant_id,
            account_id=body.account_id,
            space_id=body.space_id,
            employee_id=employee_id,
            patch=patch,
            author_id=principal.user_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.character_draft_created",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=version.id,
        purpose="ai_employee_configure",
        meta={"employee_id": employee_id, "version": version.version, "checksum": version.checksum},
    )
    return _character_version_out(version)


@router.post(
    "/agents/{employee_id}/character/versions/{version_id}/publish",
    response_model=AiEmployeeCharacterVersionOut,
)
def publish_ai_employee_character(
    employee_id: str,
    version_id: str,
    body: AiEmployeeCharacterPublish,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_configure",
        get_platform_store(), admin_required=True,
    )
    store = get_ai_employee_store()
    version = store.get_character_version(
        version_id,
        tenant_id=principal.tenant_id,
        account_id=body.account_id,
        space_id=body.space_id,
    )
    if not version or version.employee_id != employee_id:
        raise HTTPException(status_code=404, detail="AI employee character version not found.")
    try:
        published = publish_character_version(
            store,
            version_id,
            tenant_id=principal.tenant_id,
            account_id=body.account_id,
            space_id=body.space_id,
            actor_id=principal.user_id,
            expected_profile_version_id=body.expected_profile_version_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.character_published",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=published.id,
        purpose="ai_employee_configure",
        meta={"employee_id": employee_id, "version": published.version, "checksum": published.checksum},
    )
    return _character_version_out(published)


@router.post(
    "/agents/{employee_id}/character/rollback",
    response_model=AiEmployeeCharacterVersionOut,
)
def rollback_ai_employee_character(
    employee_id: str,
    body: AiEmployeeCharacterRollback,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_configure",
        get_platform_store(), admin_required=True,
    )
    try:
        published = rollback_character(
            get_ai_employee_store(),
            tenant_id=principal.tenant_id,
            account_id=body.account_id,
            space_id=body.space_id,
            employee_id=employee_id,
            source_version_id=body.source_version_id,
            actor_id=principal.user_id,
            expected_profile_version_id=body.expected_profile_version_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.character_rolled_back",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=published.id,
        purpose="ai_employee_configure",
        meta={"employee_id": employee_id, "source_version_id": body.source_version_id},
    )
    return _character_version_out(published)


@router.post(
    "/agents/{employee_id}/character/reset",
    response_model=AiEmployeeCharacterVersionOut,
)
def reset_ai_employee_character(
    employee_id: str,
    body: AiEmployeeCharacterPublish,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_configure",
        get_platform_store(), admin_required=True,
    )
    try:
        published = reset_character(
            get_ai_employee_store(),
            tenant_id=principal.tenant_id,
            account_id=body.account_id,
            space_id=body.space_id,
            employee_id=employee_id,
            actor_id=principal.user_id,
            expected_profile_version_id=body.expected_profile_version_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.character_reset",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=published.id,
        purpose="ai_employee_configure",
        meta={"employee_id": employee_id, "version": published.version},
    )
    return _character_version_out(published)


@router.get("/memories", response_model=list[AiEmployeeMemoryOut])
def list_ai_employee_memories(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    employee_id: str = "",
    status: str = "",
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    rows = get_ai_employee_store().list_memories(
        tenant_id=principal.tenant_id,
        account_id=account_id,
        space_id=space_id,
        employee_id=employee_id,
        status=status,
    )
    return [_memory_out(row) for row in rows]


@router.post("/memories", response_model=AiEmployeeMemoryOut)
def create_ai_employee_memory(
    body: AiEmployeeMemoryCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_mission_run",
        get_platform_store(),
    )
    try:
        memory = create_memory_candidate(
            get_ai_employee_store(),
            get_intake_store(),
            tenant_id=principal.tenant_id,
            account_id=body.account_id,
            space_id=body.space_id,
            employee_id=body.employee_id,
            content=body.content,
            source_refs=tuple(body.source_refs),
            classification=body.classification,
            retention_until=body.retention_until,
            author_id=principal.user_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.memory_proposed",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=memory.id,
        purpose="ai_employee_mission_run",
        meta={
            "employee_id": memory.employee_id,
            "classification": memory.classification,
            "source_count": len(memory.source_refs),
        },
    )
    return _memory_out(memory)


@router.post("/memories/{memory_id}/decision", response_model=AiEmployeeMemoryOut)
def decide_ai_employee_memory(
    memory_id: str,
    body: AiEmployeeMemoryDecision,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_configure",
        get_platform_store(), admin_required=True,
    )
    try:
        memory = decide_memory(
            get_ai_employee_store(),
            memory_id,
            tenant_id=principal.tenant_id,
            account_id=body.account_id,
            space_id=body.space_id,
            decision=body.decision,
            actor_id=principal.user_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action=f"ai_employee.memory_{body.decision}",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=memory.id,
        purpose="ai_employee_configure",
        meta={"employee_id": memory.employee_id, "decision": body.decision},
    )
    return _memory_out(memory)


@router.delete("/memories/{memory_id}", response_model=AiEmployeeMemoryOut)
def delete_ai_employee_memory(
    memory_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, account_id, space_id, "ai_employee_configure",
        get_platform_store(), admin_required=True,
    )
    try:
        memory = delete_memory(
            get_ai_employee_store(), memory_id,
            tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.memory_deleted",
        account_id=account_id,
        space_id=space_id,
        target_id=memory.id,
        purpose="ai_employee_configure",
        meta={"employee_id": memory.employee_id},
    )
    return _memory_out(memory)


@router.post("/work-products", response_model=AiEmployeeWorkProductOut)
def create_ai_employee_work_product(
    body: AiEmployeeWorkProductCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_action_propose",
        get_platform_store(),
    )
    _ensure_seeded(principal, body.account_id, body.space_id)
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        result = get_ai_employee_action_service().create_work_product(
            principal=scoped,
            employee_id=body.employee_id,
            record_type=body.record_type,
            title=body.title,
            content=body.content,
            classification=body.classification,
            source_record_ids=tuple(body.source_record_ids),
            mission_id=body.mission_id,
            conversation_id=body.conversation_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.work_product_created",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=result.record.id,
        purpose="ai_employee_action_propose",
        meta={
            "employee_id": body.employee_id,
            "record_type": body.record_type,
            "classification": body.classification,
            "source_count": len(body.source_record_ids),
        },
    )
    return _work_product_out(result.record)


@router.get("/work-products", response_model=list[AiEmployeeWorkProductOut])
def list_ai_employee_work_products(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    employee_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    rows = get_intake_store().list_by_scope(principal.tenant_id, account_id, space_id)
    return [
        _work_product_out(row)
        for row in rows
        if row.app_id == "ai_employees"
        and row.record_type in WORK_PRODUCT_RECORD_TYPES
        and row.metadata.get("generated_by_employee_id")
        and (not employee_id or row.metadata.get("generated_by_employee_id") == employee_id)
        and _can_view_classification(principal, row.classification)
    ]


@router.post("/actions", response_model=AiEmployeeActionOut)
def create_ai_employee_action(
    body: AiEmployeeActionCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_action_propose",
        get_platform_store(),
    )
    _ensure_seeded(principal, body.account_id, body.space_id)
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        proposal = get_ai_employee_action_service().propose(
            principal=scoped,
            employee_id=body.employee_id,
            action_type=body.action_type,
            target_system=body.target_system,
            risk_level=body.risk_level,
            classification=body.classification,
            actionability=body.actionability,
            source_record_ids=tuple(body.source_record_ids),
            payload_summary=body.payload_summary,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
            expires_at=body.expires_at,
            mission_id=body.mission_id,
            conversation_id=body.conversation_id,
            run_id=body.run_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.action_proposed",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=proposal.id,
        purpose="ai_employee_action_propose",
        meta={
            "employee_id": proposal.employee_id,
            "action_type": proposal.action_type,
            "target_system": proposal.target_system,
            "risk_level": proposal.risk_level,
            "classification": proposal.classification,
            "payload_hash": proposal.payload_hash,
        },
    )
    return _action_out(proposal)


@router.get("/actions", response_model=list[AiEmployeeActionOut])
def list_ai_employee_actions(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    status: str = "",
    employee_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    rows = get_ai_employee_store().list_action_proposals(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id, status=status,
    )
    return [
        _action_out(row) for row in rows
        if (not employee_id or row.employee_id == employee_id)
        and _can_view_classification(principal, row.classification)
    ]


@router.get("/actions/{proposal_id}", response_model=AiEmployeeActionOut)
def get_ai_employee_action(
    proposal_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    proposal = get_ai_employee_store().get_action_proposal(
        proposal_id, tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    if not proposal or not _can_view_classification(principal, proposal.classification):
        raise HTTPException(status_code=404, detail="AI employee action proposal not found.")
    return _action_out(proposal)


@router.post("/actions/{proposal_id}/decision", response_model=AiEmployeeActionOut)
def decide_ai_employee_action(
    proposal_id: str,
    body: AiEmployeeActionDecision,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_action_approve",
        get_platform_store(),
    )
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        proposal = get_ai_employee_action_service().decide(
            principal=scoped, proposal_id=proposal_id, decision=body.decision, note=body.note,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action=f"ai_employee.action_{body.decision}",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=proposal.id,
        purpose="ai_employee_action_approve",
        meta={
            "employee_id": proposal.employee_id,
            "action_type": proposal.action_type,
            "risk_level": proposal.risk_level,
            "payload_hash": proposal.payload_hash,
        },
    )
    return _action_out(proposal)


@router.post("/actions/{proposal_id}/execute", response_model=AiEmployeeActionOut)
def execute_ai_employee_action(
    proposal_id: str,
    body: AiEmployeeActionScope,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_action_execute",
        get_platform_store(),
    )
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        proposal = get_ai_employee_action_service().execute(
            principal=scoped, proposal_id=proposal_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action=f"ai_employee.action_{proposal.status}",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=proposal.id,
        purpose="ai_employee_action_execute",
        meta={
            "employee_id": proposal.employee_id,
            "action_type": proposal.action_type,
            "target_system": proposal.target_system,
            "payload_hash": proposal.payload_hash,
            "status": proposal.status,
        },
    )
    return _action_out(proposal)


@router.get("/connectors", response_model=list[AiConnectorBindingOut])
def list_ai_employee_connectors(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    rows = get_ai_employee_store().list_connector_bindings(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    return [_connector_binding_out(row) for row in rows]


@router.get("/connectors/health", response_model=list[AiConnectorHealthOut])
def get_ai_employee_connector_health(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    return [AiConnectorHealthOut(**get_ai_employee_google_calendar_connector().health())]


@router.post(
    "/connectors/google-calendar/oauth/start",
    response_model=GoogleCalendarOAuthStartOut,
)
def start_google_calendar_oauth(
    body: GoogleCalendarOAuthStart,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_connector_manage",
        get_platform_store(), admin_required=True,
    )
    try:
        started = get_ai_employee_google_calendar_connector().start_oauth(
            principal=principal,
            account_id=body.account_id,
            space_id=body.space_id,
            employee_ids=tuple(body.employee_ids),
            capabilities=tuple(body.capabilities),
            resource_ids=tuple(body.resource_ids),
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    return GoogleCalendarOAuthStartOut(
        authorization_url=started.authorization_url,
        state_expires_at=started.state_expires_at,
    )


@router.post(
    "/connectors/google-calendar/oauth/callback",
    response_model=AiConnectorBindingOut,
)
def complete_google_calendar_oauth(
    body: GoogleCalendarOAuthCallback,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_connector_manage",
        get_platform_store(), admin_required=True,
    )
    try:
        binding = get_ai_employee_google_calendar_connector().complete_oauth(
            principal=principal,
            state=body.state,
            code=body.code,
            expected_account_id=body.account_id,
            expected_space_id=body.space_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    if binding.account_id != body.account_id or binding.space_id != body.space_id:
        raise HTTPException(status_code=403, detail="Google Calendar OAuth scope mismatch.")
    _record_audit(
        principal,
        action="ai_employee.google_calendar_connected",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=binding.id,
        purpose="ai_employee_connector_manage",
        meta={
            "provider": binding.provider,
            "resource_count": len(binding.resource_ids),
            "employee_count": len(binding.employee_ids),
            "capabilities": list(binding.capabilities),
        },
    )
    return _connector_binding_out(binding)


@router.get(
    "/connectors/google-calendar/{binding_id}/calendars",
    response_model=list[GoogleCalendarOut],
)
def list_google_calendars(
    binding_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, account_id, space_id, "ai_employee_connector_manage",
        get_platform_store(), admin_required=True,
    )
    binding = _connector_binding_or_404(binding_id, principal, account_id, space_id)
    try:
        rows = get_ai_employee_google_calendar_connector().list_calendars(binding)
    except Exception as exc:
        _raise_runtime_error(exc)
    return [GoogleCalendarOut(**row) for row in rows]


@router.patch(
    "/connectors/google-calendar/{binding_id}",
    response_model=AiConnectorBindingOut,
)
def configure_google_calendar_binding(
    binding_id: str,
    body: GoogleCalendarBindingUpdate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_connector_manage",
        get_platform_store(), admin_required=True,
    )
    binding = _connector_binding_or_404(
        binding_id, principal, body.account_id, body.space_id,
    )
    try:
        saved = get_ai_employee_google_calendar_connector().configure_binding(
            binding,
            employee_ids=tuple(body.employee_ids),
            capabilities=tuple(body.capabilities),
            resource_ids=tuple(body.resource_ids),
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.google_calendar_grants_changed",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=saved.id,
        purpose="ai_employee_connector_manage",
        meta={
            "resource_count": len(saved.resource_ids),
            "employee_count": len(saved.employee_ids),
            "capabilities": list(saved.capabilities),
        },
    )
    return _connector_binding_out(saved)


@router.delete(
    "/connectors/google-calendar/{binding_id}",
    response_model=AiConnectorBindingOut,
)
def revoke_google_calendar_binding(
    binding_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, account_id, space_id, "ai_employee_connector_manage",
        get_platform_store(), admin_required=True,
    )
    binding = _connector_binding_or_404(binding_id, principal, account_id, space_id)
    try:
        saved = get_ai_employee_google_calendar_connector().revoke(binding)
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.google_calendar_revoked",
        account_id=account_id,
        space_id=space_id,
        target_id=saved.id,
        purpose="ai_employee_connector_manage",
        meta={"provider": saved.provider, "status": saved.status},
    )
    return _connector_binding_out(saved)


@router.post("/missions", response_model=AiMissionDetailOut)
def create_ai_employee_mission(
    body: AiMissionCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_mission_run",
        get_platform_store(),
    )
    _ensure_seeded(principal, body.account_id, body.space_id)
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        mission, _ = get_ai_employee_mission_service().create_mission(
            principal=scoped,
            account_id=body.account_id,
            space_id=body.space_id,
            goal=body.goal,
            accountable_employee_id=body.accountable_employee_id,
            participant_ids=tuple(body.participant_ids),
            token_budget=body.token_budget,
            time_budget_seconds=body.time_budget_seconds,
            cost_budget_usd=body.cost_budget_usd,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.mission_created",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=mission.id,
        purpose="ai_employee_mission_run",
        meta={
            "accountable_employee_id": mission.accountable_employee_id,
            "participant_count": len(body.participant_ids),
            "token_budget": mission.token_budget,
            "time_budget_seconds": mission.time_budget_seconds,
            "cost_budget_usd": mission.cost_budget_usd,
        },
    )
    return _mission_out(mission, get_ai_employee_store(), detail=True)


@router.get("/missions", response_model=list[AiMissionOut])
def list_ai_employee_missions(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    store = get_ai_employee_store()
    rows = store.list_missions(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    return [_mission_out(row, store) for row in rows]


@router.get("/missions/{mission_id}", response_model=AiMissionDetailOut)
def get_ai_employee_mission(
    mission_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_reader(principal, account_id, space_id, get_platform_store())
    store = get_ai_employee_store()
    mission = store.get_mission(
        mission_id, tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    )
    if not mission:
        raise HTTPException(status_code=404, detail="AI employee mission not found.")
    return _mission_out(mission, store, detail=True)


@router.post("/missions/{mission_id}/run")
def stream_ai_employee_mission(
    mission_id: str,
    body: AiMissionRunRequest,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_mission_run",
        get_platform_store(),
    )
    scoped = _runtime_principal(principal, body.account_id, body.space_id)

    def event_stream():
        try:
            for event in get_ai_employee_mission_service().run_mission(
                principal=scoped, mission_id=mission_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            message, code = _runtime_error_payload(exc)
            yield f"data: {json.dumps({'type': 'error', 'code': code, 'message': message})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/missions/{mission_id}/cancel", response_model=AiMissionOut)
def cancel_ai_employee_mission(
    mission_id: str,
    body: AiMissionRunRequest,
    principal: Principal = Depends(resolve_principal),
):
    authorize_ai_employee_purpose(
        principal, body.account_id, body.space_id, "ai_employee_mission_run",
        get_platform_store(),
    )
    scoped = _runtime_principal(principal, body.account_id, body.space_id)
    try:
        mission = get_ai_employee_mission_service().cancel_mission(
            principal=scoped, mission_id=mission_id,
        )
    except Exception as exc:
        _raise_runtime_error(exc)
    _record_audit(
        principal,
        action="ai_employee.mission_cancelled",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=mission.id,
        purpose="ai_employee_mission_run",
        meta={"phase": mission.phase},
    )
    return _mission_out(mission, get_ai_employee_store())


def _mission_out(mission, store, *, detail: bool = False):
    scope = {
        "tenant_id": mission.tenant_id,
        "account_id": mission.account_id,
        "space_id": mission.space_id,
    }
    participants = store.list_mission_participants(mission.id, **scope)
    runs = [row for row in store.list_runs(**scope) if row.mission_id == mission.id]
    values = {
        "id": mission.id,
        "account_id": mission.account_id,
        "space_id": mission.space_id,
        "goal": mission.goal,
        "sponsor_id": mission.sponsor_id,
        "accountable_employee_id": mission.accountable_employee_id,
        "status": mission.status,
        "phase": mission.phase,
        "token_budget": mission.token_budget,
        "time_budget_seconds": mission.time_budget_seconds,
        "cost_budget_usd": mission.cost_budget_usd,
        "synthesis_message_id": mission.synthesis_message_id,
        "error": mission.error,
        "created_at": mission.created_at,
        "updated_at": mission.updated_at,
        "participants": [AiMissionParticipantOut(
            employee_id=row.employee_id,
            mission_role=row.mission_role,
            character_version_id=row.character_version_id,
            model_policy_id=row.model_policy_id,
            status=row.status,
        ) for row in participants],
        "usage": AiMissionUsageOut(
            prompt_tokens=sum(row.prompt_tokens for row in runs),
            completion_tokens=sum(row.completion_tokens for row in runs),
            cost_usd=sum(row.cost_usd for row in runs),
        ),
    }
    if not detail:
        return AiMissionOut(**values)
    conversation = next((row for row in store.list_conversations(
        **scope, human_owner_id=mission.sponsor_id,
    ) if row.mission_id == mission.id), None)
    messages = store.list_messages(conversation.id, **scope) if conversation else []
    return AiMissionDetailOut(
        **values,
        conversation_id=conversation.id if conversation else "",
        messages=[_message_out(row) for row in messages],
    )


def _work_product_out(record) -> AiEmployeeWorkProductOut:
    return AiEmployeeWorkProductOut(
        id=record.id,
        employee_id=str(record.metadata.get("generated_by_employee_id") or ""),
        record_type=record.record_type,
        title=record.title,
        content=record.content,
        classification=record.classification,
        source_record_ids=list(record.extracted_facts.get("source_record_ids") or []),
        mission_id=str(record.metadata.get("mission_id") or ""),
        conversation_id=str(record.metadata.get("conversation_id") or ""),
        created_at=record.created_at,
    )


def _action_out(proposal) -> AiEmployeeActionOut:
    return AiEmployeeActionOut(
        id=proposal.id,
        mission_id=proposal.mission_id,
        conversation_id=proposal.conversation_id,
        run_id=proposal.run_id,
        employee_id=proposal.employee_id,
        action_type=proposal.action_type,
        target_system=proposal.target_system,
        risk_level=proposal.risk_level,
        classification=proposal.classification,
        actionability=proposal.actionability,
        source_record_ids=list(proposal.source_record_ids),
        payload_summary=proposal.payload_summary,
        payload=proposal.payload,
        payload_hash=proposal.payload_hash,
        required_approver_role=proposal.required_approver_role,
        expires_at=proposal.expires_at,
        idempotency_key=proposal.idempotency_key,
        status=proposal.status,
        requires_approval=proposal.requires_approval,
        reason=proposal.reason,
        approved_by=proposal.approved_by,
        approved_at=proposal.approved_at,
        execution_ref=proposal.execution_ref,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def _can_view_classification(principal: Principal, classification: str) -> bool:
    return Classification.parse(classification) <= principal.clearance


def _connector_binding_out(binding) -> AiConnectorBindingOut:
    return AiConnectorBindingOut(
        id=binding.id,
        provider=binding.provider,
        resource_type=binding.resource_type,
        resource_ids=list(binding.resource_ids),
        employee_ids=list(binding.employee_ids),
        capabilities=list(binding.capabilities),
        status=binding.status,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _connector_binding_or_404(
    binding_id: str,
    principal: Principal,
    account_id: str,
    space_id: str,
):
    binding = next((row for row in get_ai_employee_store().list_connector_bindings(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    ) if row.id == binding_id and row.provider == "google_calendar"), None)
    if not binding:
        raise HTTPException(status_code=404, detail="Google Calendar binding not found.")
    return binding


def _agent_out(profile: AiEmployeeProfile, store) -> AiEmployeeOut:
    employee = get_ai_employee(profile.employee_id)
    scope = {
        "tenant_id": profile.tenant_id,
        "account_id": profile.account_id,
        "space_id": profile.space_id,
    }
    version = store.get_character_version(profile.default_version_id, **scope)
    policy = store.get_model_policy(profile.employee_id, **scope)
    if not version or not policy:
        raise HTTPException(status_code=500, detail="AI employee configuration is incomplete.")
    payload = version.payload
    return AiEmployeeOut(
        profile_id=profile.id,
        employee_id=profile.employee_id,
        name=str(payload.get("display_name") or employee.name),
        fictional_age=int(payload.get("fictional_age") or employee.age),
        country=str(payload.get("country") or employee.country),
        pronouns=str(payload.get("pronouns") or employee.pronouns),
        role=profile.role,
        department=profile.department,
        pod=profile.pod,
        reports_to=profile.reports_to,
        status=profile.status,
        leadership_council=employee.leadership_council,
        personality=list(payload.get("personality") or employee.personality),
        tone=str(payload.get("tone") or employee.tone),
        strengths=list(payload.get("strengths") or employee.strengths),
        watch_outs=list(payload.get("watch_outs") or employee.watch_outs),
        working_style=str(payload.get("working_style") or employee.working_style),
        biography=str(payload.get("biography") or employee.prompt_safe_description),
        avatar_url=str(payload.get("avatar_url") or ""),
        character_version_id=version.id,
        character_version=version.version,
        model_policy_id=policy.id,
        model_provider=policy.provider,
        model=policy.model,
        default_mode=employee.default_mode,
        safe_actions=list(employee.safe_actions),
        approval_rule=employee.approval_rule,
        productivity_metrics=list(employee.productivity_metrics),
        never_without_approval=list(employee.never_without_approval),
    )


def _ensure_seeded(principal: Principal, account_id: str, space_id: str) -> None:
    store = get_ai_employee_store()
    if store.list_profiles(
        tenant_id=principal.tenant_id, account_id=account_id, space_id=space_id,
    ):
        return
    store.seed_defaults(
        tenant_id=principal.tenant_id,
        account_id=account_id,
        space_id=space_id,
        author_id=f"system:{AI_EMPLOYEES_CONTRACT_VERSION}",
    )


def _runtime_principal(principal: Principal, account_id: str, space_id: str) -> Principal:
    return replace(principal, account_id=account_id, space_ids=frozenset({space_id}))


def _conversation_out(conversation) -> AiEmployeeConversationOut:
    return AiEmployeeConversationOut(
        id=conversation.id,
        account_id=conversation.account_id,
        space_id=conversation.space_id,
        employee_id=conversation.employee_id,
        human_owner_id=conversation.human_owner_id,
        title=conversation.title,
        status=conversation.status,
        character_version_id=conversation.character_version_id,
        model_policy_id=conversation.model_policy_id,
        mission_id=conversation.mission_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_out(message) -> AiEmployeeMessageOut:
    return AiEmployeeMessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        speaker_type=message.speaker_type,
        speaker_id=message.speaker_id,
        visibility=message.visibility,
        content=message.content,
        citations=list(message.citations),
        run_id=message.run_id,
        created_at=message.created_at,
    )


def _character_version_out(version) -> AiEmployeeCharacterVersionOut:
    return AiEmployeeCharacterVersionOut(
        id=version.id,
        employee_id=version.employee_id,
        version=version.version,
        state=version.state,
        payload=version.payload,
        checksum=version.checksum,
        author_id=version.author_id,
        base_version_id=version.base_version_id,
        created_at=version.created_at,
        published_at=version.published_at,
        preview=character_preview(version.employee_id, version.payload),
    )


def _memory_out(memory) -> AiEmployeeMemoryOut:
    return AiEmployeeMemoryOut(
        id=memory.id,
        employee_id=memory.employee_id,
        content=memory.content,
        source_refs=list(memory.source_refs),
        classification=memory.classification,
        status=memory.status,
        retention_until=memory.retention_until,
        author_id=memory.author_id,
        approved_by=memory.approved_by,
        created_at=memory.created_at,
        approved_at=memory.approved_at,
    )


def _runtime_error_payload(exc: Exception) -> tuple[str, str]:
    from app.ai_employees.backends.base import BackendUnavailableError
    from app.ai_employees.connectors.base import ConnectorRequestError, ConnectorUnavailableError

    if isinstance(exc, BackendUnavailableError):
        return str(exc), "backend_unavailable"
    if isinstance(exc, ConnectorUnavailableError):
        return str(exc), "connector_unavailable"
    if isinstance(exc, ConnectorRequestError):
        return "The external connector request failed.", "connector_failed"
    if isinstance(exc, PermissionError):
        return "This AI employee conversation is not available to the current user.", "forbidden"
    if isinstance(exc, KeyError):
        return "AI employee conversation not found.", "not_found"
    if isinstance(exc, ValueError):
        return str(exc), "invalid_request"
    return "The AI employee turn could not be started.", "runtime_failed"


def _raise_runtime_error(exc: Exception):
    from app.ai_employees.backends.base import BackendUnavailableError
    from app.ai_employees.connectors.base import ConnectorRequestError, ConnectorUnavailableError

    if isinstance(exc, BackendUnavailableError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, ConnectorUnavailableError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, ConnectorRequestError):
        raise HTTPException(status_code=502, detail="The external connector request failed.") from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def _record_audit(
    principal: Principal,
    *,
    action: str,
    account_id: str,
    space_id: str,
    target_id: str,
    purpose: str,
    meta: dict,
) -> None:
    get_platform_store().record_audit(AuditEvent(
        id=f"aud_ai_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type="ai_employee",
        target_id=target_id,
        space_id=space_id,
        app_id="ai_employees",
        purpose=purpose,
        decision="recorded",
        meta=dict(meta),
    ))

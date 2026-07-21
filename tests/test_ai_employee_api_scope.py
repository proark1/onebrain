"""Human API scope and module-installation contracts for AI Employees."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routers.ai_employees as ai_router
from app.ai_employees.backends.base import BackendEvent
from app.ai_employees.backends.registry import BackendRegistry
from app.ai_employees.base import AiEmployeeMemory
from app.ai_employees.contracts import AI_EMPLOYEE_PURPOSES, get_ai_employee
from app.ai_employees.memory import MemoryAiEmployeeStore
from app.ai_employees.missions import AiMissionService, MissionAgentResult
from app.ai_employees.actions import AiEmployeeActionService
from app.ai_employees.runtime import AiEmployeeRuntime
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.platform.base import Account, AppInstallation, Membership, Space
from app.platform.memory import MemoryPlatformStore
from app.security.policy import Classification
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore


def _human(role_id: str = "admin", user_id: str = "admin@acme", tenant_id: str = "acme") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=user_id,
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"berlin"}),
        categories=role.categories,
        location_label="all",
        tenant_id=tenant_id,
    )


def _service() -> Principal:
    return Principal(
        user_id="svc:ai",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,
        locations=frozenset(),
        categories=frozenset({"general"}),
        location_label="-",
        tenant_id="acme",
        principal_type="service",
        app_id="ai_employees",
        account_id="acme",
        space_ids=frozenset({"sp_business"}),
        purposes=AI_EMPLOYEE_PURPOSES,
    )


def _stores(*, installation_status: str = "active"):
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id="acme", kind="organization", name="Acme", owner_user_id="admin@acme",
    ))
    platform.create_space(Space(
        id="sp_business", account_id="acme", kind="business", name="Business",
    ))
    platform.create_space(Space(
        id="sp_shared", account_id="acme", kind="shared", name="Shared",
    ))
    platform.install_app(AppInstallation(
        id="appi_ai",
        account_id="acme",
        app_id="ai_employees",
        enabled_space_ids=("sp_business",),
        allowed_purposes=tuple(sorted(AI_EMPLOYEE_PURPOSES)),
        display_name="AI Employees",
        status=installation_status,
    ))
    return platform, MemoryAiEmployeeStore()


def _wire(monkeypatch, *, installation_status: str = "active"):
    platform, employees = _stores(installation_status=installation_status)
    monkeypatch.setattr(ai_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(ai_router, "get_ai_employee_store", lambda: employees)
    return platform, employees


def test_workspace_discovery_and_team_seed_are_live_and_module_scoped(monkeypatch):
    _, store = _wire(monkeypatch)

    workspaces = ai_router.list_ai_employee_workspaces(principal=_human())
    assert len(workspaces) == 1
    assert workspaces[0].space_id == "sp_business"
    assert workspaces[0].installation_status == "active"
    assert workspaces[0].can_configure is True
    assert workspaces[0].can_run_missions is True
    assert workspaces[0].can_manage_connectors is True

    team = ai_router.get_ai_employee_team(
        account_id="acme", space_id="sp_business", principal=_human(),
    )
    assert team.contract_version == "ai_employees.v2"
    assert team.max_mission_squad_size == 6
    assert len(team.agents) == 16
    assert [row.employee_id for row in team.agents[:2]] == [
        "chief_of_staff", "corporate_strategy_manager",
    ]
    assert team.leadership_council_ids == [
        "chief_of_staff",
        "chief_operating_officer",
        "chief_product_technology_officer",
        "chief_marketing_officer",
    ]
    assert team.pods["operations_corporate"] == [
        "chief_operating_officer", "operations_manager", "finance_manager",
        "legal_compliance_manager", "people_hr_manager",
    ]
    assert all(row.model_provider == "gemini" for row in team.agents)
    assert all(row.model.startswith("gemini/") for row in team.agents)
    assert len(store.list_profiles(tenant_id="acme", account_id="acme", space_id="sp_business")) == 16


def test_team_and_employee_detail_expose_safe_expanded_context_without_hidden_prompts(monkeypatch):
    _wire(monkeypatch)
    team = ai_router.get_ai_employee_team(
        account_id="acme", space_id="sp_business", principal=_human(),
    )
    expected = get_ai_employee("finance_manager")
    finance = next(agent for agent in team.agents if agent.employee_id == expected.id)
    detail = ai_router.get_ai_employee_detail(
        expected.id, account_id="acme", space_id="sp_business", principal=_human(),
    )

    for agent in (finance, detail):
        assert agent.safe_actions == list(expected.safe_actions)
        assert agent.approval_rule == expected.approval_rule
        assert agent.productivity_metrics == list(expected.productivity_metrics)
        assert agent.never_without_approval == list(expected.never_without_approval)
        assert agent.biography
        exposed = agent.model_dump()
        assert "character_prompt" not in exposed
        assert "purposes" not in exposed
        assert "owner_role" not in exposed


def test_space_member_can_read_but_only_account_admin_can_configure(monkeypatch):
    platform, _ = _wire(monkeypatch)
    platform.upsert_membership(Membership(
        id="member-1", account_id="acme", user_id="member@acme", role_id="viewer",
        space_id="sp_business",
    ))
    member = _human(role_id="finance", user_id="member@acme")

    workspaces = ai_router.list_ai_employee_workspaces(principal=member)
    assert [(row.space_id, row.can_configure) for row in workspaces] == [("sp_business", False)]
    assert len(ai_router.get_ai_employee_team(
        account_id="acme", space_id="sp_business", principal=member,
    ).agents) == 16

    with pytest.raises(HTTPException) as denied:
        ai_router.set_ai_employee_status(
            "finance_manager",
            ai_router.AiEmployeeStatusUpdate(account_id="acme", space_id="sp_business", status="paused"),
            principal=member,
        )
    assert denied.value.status_code == 403


def test_paused_module_is_read_only_and_does_not_seed_new_state(monkeypatch):
    _, store = _wire(monkeypatch, installation_status="paused")
    store.seed_defaults(
        tenant_id="acme", account_id="acme", space_id="sp_business", author_id="system:test",
    )

    workspaces = ai_router.list_ai_employee_workspaces(principal=_human())
    assert workspaces[0].installation_status == "paused"
    assert workspaces[0].can_configure is False
    assert workspaces[0].can_run_missions is False
    assert len(ai_router.get_ai_employee_team(
        account_id="acme", space_id="sp_business", principal=_human(),
    ).agents) == 16

    with pytest.raises(HTTPException) as denied:
        ai_router.set_ai_employee_status(
            "finance_manager",
            ai_router.AiEmployeeStatusUpdate(account_id="acme", space_id="sp_business", status="paused"),
            principal=_human(),
        )
    assert denied.value.status_code == 403


def test_cross_account_uninstalled_space_and_service_principal_fail_closed(monkeypatch):
    _wire(monkeypatch)

    with pytest.raises(HTTPException) as cross:
        ai_router.get_ai_employee_team(
            account_id="acme", space_id="sp_business",
            principal=_human(user_id="admin@other", tenant_id="other"),
        )
    assert cross.value.status_code == 404

    with pytest.raises(HTTPException) as uninstalled:
        ai_router.get_ai_employee_team(
            account_id="acme", space_id="sp_shared", principal=_human(),
        )
    assert uninstalled.value.status_code == 403

    with pytest.raises(HTTPException) as service:
        ai_router.list_ai_employee_workspaces(principal=_service())
    assert service.value.status_code == 403


def test_admin_can_pause_and_resume_an_employee_without_editing_role_policy(monkeypatch):
    _, _ = _wire(monkeypatch)
    admin = _human()
    ai_router.get_ai_employee_team(account_id="acme", space_id="sp_business", principal=admin)

    paused = ai_router.set_ai_employee_status(
        "finance_manager",
        ai_router.AiEmployeeStatusUpdate(account_id="acme", space_id="sp_business", status="paused"),
        principal=admin,
    )
    assert paused.status == "paused"
    assert paused.role == "Finance Manager"
    resumed = ai_router.set_ai_employee_status(
        "finance_manager",
        ai_router.AiEmployeeStatusUpdate(
            account_id="acme", space_id="sp_business", status="active",
        ),
        principal=admin,
    )
    assert resumed.status == "active"


def test_direct_conversation_api_streams_sse_and_lists_persisted_messages(monkeypatch):
    _, store = _wire(monkeypatch)

    class Retrieval:
        def retrieve(self, principal, question):
            return []

    class Backend:
        provider = "gemini"
        available = True
        unavailable_reason = ""

        def stream(self, request):
            yield BackendEvent(type="text", text="Clara has prepared the next steps.")
            yield BackendEvent(type="usage", prompt_tokens=20, completion_tokens=8, cost_usd=0.00001)
            yield BackendEvent(type="done")

    registry = BackendRegistry([Backend()])
    runtime = AiEmployeeRuntime(
        store=store, retrieval_service=Retrieval(), backend_registry=registry,
    )
    monkeypatch.setattr(ai_router, "get_ai_employee_runtime", lambda: runtime)
    monkeypatch.setattr(ai_router, "get_ai_employee_backend_registry", lambda: registry)
    admin = _human()

    created = ai_router.create_ai_employee_conversation(
        ai_router.AiEmployeeConversationCreate(
            account_id="acme", space_id="sp_business", employee_id="chief_of_staff", title="Plan",
        ),
        principal=admin,
    )
    assert ai_router.list_ai_employee_conversations(
        account_id="acme", space_id="sp_business", principal=admin,
    )[0].id == created.id

    response = ai_router.stream_ai_employee_turn(
        created.id,
        ai_router.AiEmployeeTurnCreate(
            account_id="acme", space_id="sp_business", question="Plan the week.",
            idempotency_key="api-turn-1",
        ),
        principal=admin,
    )

    async def collect() -> str:
        parts = []
        async for chunk in response.body_iterator:
            parts.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(parts)

    stream = asyncio.run(collect())
    assert '"type": "text"' in stream
    assert "Clara has prepared the next steps." in stream
    assert '"type": "done"' in stream
    messages = ai_router.list_ai_employee_messages(
        created.id, account_id="acme", space_id="sp_business", principal=admin,
    )
    assert [message.speaker_type for message in messages] == ["human", "employee"]
    assert ai_router.get_ai_employee_models(
        account_id="acme", space_id="sp_business", principal=admin,
    ).health[0].provider == "gemini"


def test_admin_character_draft_preview_publish_and_schema_guard(monkeypatch):
    _, store = _wire(monkeypatch)
    admin = _human()
    ai_router.get_ai_employee_team(
        account_id="acme", space_id="sp_business", principal=admin,
    )
    profile = store.get_profile(
        "finance_manager", tenant_id="acme", account_id="acme", space_id="sp_business",
    )
    draft = ai_router.create_ai_employee_character_draft(
        "finance_manager",
        ai_router.AiEmployeeCharacterPatch(
            account_id="acme", space_id="sp_business", display_name="Sophie L.",
            tone="Calm and compact.",
        ),
        principal=admin,
    )
    assert draft.state == "draft"
    assert "Finance Manager" in draft.preview
    published = ai_router.publish_ai_employee_character(
        "finance_manager",
        draft.id,
        ai_router.AiEmployeeCharacterPublish(
            account_id="acme", space_id="sp_business",
            expected_profile_version_id=profile.default_version_id,
        ),
        principal=admin,
    )
    assert published.state == "published"
    assert ai_router.get_ai_employee_team(
        account_id="acme", space_id="sp_business", principal=admin,
    ).agents[4].name == "Sophie L."
    assert len(ai_router.list_ai_employee_character_versions(
        "finance_manager", account_id="acme", space_id="sp_business", principal=admin,
    )) == 2

    with pytest.raises(ValidationError):
        ai_router.AiEmployeeCharacterPatch(
            account_id="acme", space_id="sp_business", role="Autonomous CFO",
        )


def test_mission_api_creates_streams_and_exposes_separate_employee_turns(monkeypatch):
    _, store = _wire(monkeypatch)

    class Retrieval:
        def retrieve(self, principal, question):
            return []

    class Executor:
        def generate(self, request):
            return MissionAgentResult(
                content=f"{request.phase} from {request.employee_id}",
                prompt_tokens=10,
                completion_tokens=5,
                cost_usd=0.001,
                backend="gemini",
                model="gemini/gemini-2.5-flash",
            )

    service = AiMissionService(
        store=store, retrieval_service=Retrieval(), executor=Executor(),
    )
    monkeypatch.setattr(ai_router, "get_ai_employee_mission_service", lambda: service)
    admin = _human()
    created = ai_router.create_ai_employee_mission(
        ai_router.AiMissionCreate(
            account_id="acme",
            space_id="sp_business",
            goal="Prepare the launch operating plan.",
            accountable_employee_id="chief_operating_officer",
            participant_ids=[
                "chief_of_staff", "chief_operating_officer", "finance_manager",
            ],
        ),
        principal=admin,
    )
    assert created.status == "draft"
    assert [row.mission_role for row in created.participants] == [
        "orchestrator", "accountable", "specialist",
    ]

    response = ai_router.stream_ai_employee_mission(
        created.id,
        ai_router.AiMissionRunRequest(account_id="acme", space_id="sp_business"),
        principal=admin,
    )

    async def collect() -> str:
        parts = []
        async for chunk in response.body_iterator:
            parts.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(parts)

    stream = asyncio.run(collect())
    assert '"phase": "scope"' in stream
    assert '"phase": "challenge"' in stream
    assert '"type": "mission_done"' in stream
    detail = ai_router.get_ai_employee_mission(
        created.id, account_id="acme", space_id="sp_business", principal=admin,
    )
    assert detail.status == "completed"
    assert len(detail.messages) == 7
    assert detail.usage.prompt_tokens == 70
    assert ai_router.list_ai_employee_missions(
        account_id="acme", space_id="sp_business", principal=admin,
    )[0].id == created.id


def test_memory_listing_applies_the_caller_clearance_ceiling(monkeypatch):
    """A memory can sit above the reader's clearance, so the lister must filter.

    authorize_ai_employee_reader is space membership plus app purpose; it has no
    clearance dimension. Memories must carry at least their source's
    classification, so without this ceiling any space member reads `restricted`
    content verbatim -- while the work-product, action, and proposal listers in
    this same router all apply it.
    """
    platform, employees = _wire(monkeypatch)
    platform.upsert_membership(Membership(
        id="m_frontdesk", account_id="acme", user_id="frontdesk@acme",
        role_id="front_desk", space_id="sp_business",
    ))
    scope = {"tenant_id": "acme", "account_id": "acme", "space_id": "sp_business"}
    retention = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    for suffix, classification in (("internal", "internal"), ("restricted", "restricted")):
        employees.save_memory(AiEmployeeMemory(
            id=f"mem-{suffix}", **scope, employee_id="finance_manager",
            content=f"{classification} board detail", source_refs=("rec-1",),
            classification=classification, status="approved",
            retention_until=retention, author_id="admin@acme",
            approved_by="admin@acme",
            approved_at=datetime.now(timezone.utc).isoformat(),
        ))

    admin = _human()                                   # RESTRICTED clearance
    front_desk = _human(role_id="front_desk", user_id="frontdesk@acme")  # INTERNAL

    visible_to_admin = ai_router.list_ai_employee_memories(
        account_id="acme", space_id="sp_business", principal=admin,
    )
    assert {row.id for row in visible_to_admin} == {"mem-internal", "mem-restricted"}

    visible_to_front_desk = ai_router.list_ai_employee_memories(
        account_id="acme", space_id="sp_business", principal=front_desk,
    )
    assert {row.id for row in visible_to_front_desk} == {"mem-internal"}
    assert all("restricted board detail" not in row.content for row in visible_to_front_desk)


def test_work_product_and_action_queue_api_preserve_sources_hash_and_fresh_approval(monkeypatch):
    _, employees = _wire(monkeypatch)
    intake = MemoryIntakeStore()
    intake.create(IntakeRecord(
        id="source-1", tenant_id="acme", account_id="acme", space_id="sp_business",
        app_id="core", purpose="knowledge", source="upload", source_ref="source-1",
        record_type="document", intent="knowledge_update", classification="internal",
        confidence=1.0, status="approved", title="Launch context", content="Approved context",
        summary="Context", metadata={"category": "general"},
    ))
    sessions = MemorySessionStore()
    sessions.create(Session(
        id="fresh-session", user_id="admin@acme", tenant_id="acme",
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    ))
    service = AiEmployeeActionService(
        store=employees, intake_store=intake, session_store=sessions,
    )
    monkeypatch.setattr(ai_router, "get_ai_employee_action_service", lambda: service)
    monkeypatch.setattr(ai_router, "get_intake_store", lambda: intake)
    admin = replace(_human(), session_id="fresh-session")

    work = ai_router.create_ai_employee_work_product(
        ai_router.AiEmployeeWorkProductCreate(
            account_id="acme", space_id="sp_business", employee_id="finance_manager",
            record_type="brief", title="Finance launch brief", content="Approved finance view.",
            classification="internal", source_record_ids=["source-1"],
        ),
        principal=admin,
    )
    assert work.employee_id == "finance_manager"
    assert ai_router.list_ai_employee_work_products(
        account_id="acme", space_id="sp_business", principal=admin,
    )[0].id == work.id

    action = ai_router.create_ai_employee_action(
        ai_router.AiEmployeeActionCreate(
            account_id="acme", space_id="sp_business", employee_id="chief_of_staff",
            action_type="calendar_create_event", target_system="google_calendar",
            risk_level="medium", classification="internal", actionability="approval_required",
            source_record_ids=["source-1"], payload_summary="Create launch meeting",
            payload={"calendar_id": "primary", "summary": "Launch"},
            idempotency_key="launch-calendar-api",
        ),
        principal=admin,
    )
    assert action.status == "proposed"
    assert len(action.payload_hash) == 64
    approved = ai_router.decide_ai_employee_action(
        action.id,
        ai_router.AiEmployeeActionDecision(
            account_id="acme", space_id="sp_business", decision="approved",
        ),
        principal=admin,
    )
    assert approved.status == "approved"
    assert approved.approved_by == "admin@acme"
    assert ai_router.list_ai_employee_actions(
        account_id="acme", space_id="sp_business", status="approved", principal=admin,
    )[0].id == action.id

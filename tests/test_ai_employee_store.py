"""Persistence contracts for the AI Employees module."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from app.ai_employees.base import (
    AiActionProposalRecord,
    AiAgentRun,
    AiConnectorBinding,
    AiEmployeeConversation,
    AiEmployeeMemory,
    AiEmployeeMessage,
    AiMission,
    AiMissionParticipant,
)
from app.ai_employees.memory import MemoryAiEmployeeStore


SCOPE = {"tenant_id": "tenant-a", "account_id": "acme", "space_id": "business"}


def test_seed_defaults_is_idempotent_and_creates_published_characters_and_gemini_policies():
    store = MemoryAiEmployeeStore()

    first = store.seed_defaults(**SCOPE, author_id="admin-1")
    second = store.seed_defaults(**SCOPE, author_id="admin-2")

    assert len(first) == 16
    assert second == first
    assert len(store.list_profiles(**SCOPE)) == 16
    assert len(store.list_character_versions(**SCOPE)) == 16
    assert len(store.list_model_policies(**SCOPE)) == 16
    assert store.list_profiles(tenant_id="tenant-a", account_id="other", space_id="business") == []

    for profile in first:
        assert profile.default_version_id
        version = store.get_character_version(profile.default_version_id, **SCOPE)
        assert version is not None
        assert version.state == "published"
        assert version.version == 1
        assert version.checksum
        assert version.payload["display_name"]
        policy = store.get_model_policy(profile.employee_id, **SCOPE)
        assert policy is not None
        assert policy.provider == "gemini"
        assert policy.model == "gemini/gemini-2.5-flash"
        assert policy.version == 1
        assert policy.status == "active"


def test_character_draft_publish_pins_new_version_and_published_versions_are_immutable():
    store = MemoryAiEmployeeStore()
    profiles = store.seed_defaults(**SCOPE, author_id="admin-1")
    clara = next(profile for profile in profiles if profile.employee_id == "chief_of_staff")

    draft = store.create_character_draft(
        **SCOPE,
        employee_id="chief_of_staff",
        payload={"display_name": "Clara Hoffmann", "tone": "Even more concise", "character_prompt": "Stay concise."},
        author_id="admin-1",
        base_version_id=clara.default_version_id,
    )
    assert draft.state == "draft"
    assert draft.version == 2
    assert store.get_profile("chief_of_staff", **SCOPE).default_version_id == clara.default_version_id

    published = store.publish_character_version(
        draft.id,
        **SCOPE,
        actor_id="admin-1",
        expected_profile_version_id=clara.default_version_id,
    )
    assert published.state == "published"
    assert published.published_at
    assert store.get_profile("chief_of_staff", **SCOPE).default_version_id == draft.id

    with pytest.raises(ValueError, match="immutable"):
        store.save_character_version(replace(published, payload={"display_name": "Changed"}))

    with pytest.raises(ValueError, match="version conflict"):
        another = store.create_character_draft(
            **SCOPE,
            employee_id="chief_of_staff",
            payload={"display_name": "Clara"},
            author_id="admin-1",
            base_version_id=clara.default_version_id,
        )
        store.publish_character_version(
            another.id,
            **SCOPE,
            actor_id="admin-1",
            expected_profile_version_id=clara.default_version_id,
        )


def test_operational_records_round_trip_with_strict_scope_and_idempotency():
    store = MemoryAiEmployeeStore()
    store.seed_defaults(**SCOPE, author_id="admin-1")
    profile = store.get_profile("finance_manager", **SCOPE)
    policy = store.get_model_policy("finance_manager", **SCOPE)

    conversation = store.save_conversation(AiEmployeeConversation(
        id="conv-1", **SCOPE, employee_id="finance_manager", human_owner_id="user-1",
        title="Runway", status="active", character_version_id=profile.default_version_id,
        model_policy_id=policy.id,
    ))
    message = store.save_message(AiEmployeeMessage(
        id="msg-1", **SCOPE, conversation_id=conversation.id, speaker_type="human",
        speaker_id="user-1", visibility="shared", content="Prepare a runway report.",
        citations=("rec-1",),
    ))
    mission = store.save_mission(AiMission(
        id="mission-1", **SCOPE, goal="Prepare the board finance pack", sponsor_id="user-1",
        accountable_employee_id="chief_operating_officer", status="draft", phase="scope",
        token_budget=20_000, time_budget_seconds=600, cost_budget_usd=5.0,
    ))
    participant = store.save_mission_participant(AiMissionParticipant(
        id="participant-1", **SCOPE, mission_id=mission.id, employee_id="finance_manager",
        mission_role="specialist", character_version_id=profile.default_version_id,
        model_policy_id=policy.id, status="active",
    ))
    run = store.save_run(AiAgentRun(
        id="run-1", **SCOPE, conversation_id=conversation.id, mission_id=mission.id,
        employee_id="finance_manager", backend="gemini", model="gemini/gemini-2.5-flash",
        idempotency_key="turn-1", status="queued", input_hash="abc",
    ))
    memory = store.save_memory(AiEmployeeMemory(
        id="memory-1", **SCOPE, employee_id="finance_manager", content="Board reporting uses EUR.",
        source_refs=("rec-1",), classification="internal", status="approved",
        retention_until="2027-07-16T00:00:00+00:00", author_id="user-1", approved_by="admin-1",
    ))
    binding = store.save_connector_binding(AiConnectorBinding(
        id="binding-1", **SCOPE, provider="google_calendar", credential_ref="secret://google/acme/1",
        resource_type="calendar", resource_ids=("primary",), employee_ids=("chief_of_staff",),
        capabilities=("read_events", "create_self_focus"), status="active",
    ))
    proposal = store.save_action_proposal(AiActionProposalRecord(
        id="proposal-1", **SCOPE, mission_id=mission.id, conversation_id=conversation.id,
        run_id=run.id, employee_id="finance_manager", action_type="calendar_create_event",
        target_system="google_calendar", risk_level="medium", classification="internal",
        actionability="approval_required", source_record_ids=("rec-1",), payload_summary="Board review",
        payload={"calendar_id": "primary", "summary": "Board review"}, payload_hash="hash-1",
        required_approver_role="account_admin", expires_at="2026-07-17T00:00:00+00:00",
        idempotency_key="proposal-key-1", status="proposed", requires_approval=True,
        reason="Attendees require approval.",
    ))

    assert store.get_conversation(conversation.id, **SCOPE) == conversation
    assert store.list_messages(conversation.id, **SCOPE) == [message]
    assert store.get_mission(mission.id, **SCOPE) == mission
    assert store.list_mission_participants(mission.id, **SCOPE) == [participant]
    assert store.get_run_by_idempotency("turn-1", **SCOPE) == run
    assert store.list_memories(**SCOPE, employee_id="finance_manager") == [memory]
    assert store.list_connector_bindings(**SCOPE) == [binding]
    assert store.get_action_proposal_by_idempotency("proposal-key-1", **SCOPE) == proposal

    wrong_scope = {**SCOPE, "account_id": "other"}
    assert store.get_conversation(conversation.id, **wrong_scope) is None
    assert store.list_messages(conversation.id, **wrong_scope) == []
    assert store.get_mission(mission.id, **wrong_scope) is None
    assert store.get_run_by_idempotency("turn-1", **wrong_scope) is None
    assert store.list_memories(**wrong_scope) == []
    assert store.list_connector_bindings(**wrong_scope) == []
    assert store.get_action_proposal_by_idempotency("proposal-key-1", **wrong_scope) is None

    with pytest.raises(ValueError, match="idempotency"):
        store.save_run(replace(run, id="run-2", input_hash="different"))
    with pytest.raises(ValueError, match="Raw secret"):
        store.save_action_proposal(replace(proposal, id="proposal-2", payload={"access_token": "raw"}))


def test_message_and_mission_participant_order_do_not_depend_on_random_ids():
    store = MemoryAiEmployeeStore()
    store.seed_defaults(**SCOPE, author_id="admin-1")
    staff_profile = store.get_profile("chief_of_staff", **SCOPE)
    operating_profile = store.get_profile("chief_operating_officer", **SCOPE)
    finance_profile = store.get_profile("finance_manager", **SCOPE)
    staff_policy = store.get_model_policy("chief_of_staff", **SCOPE)
    operating_policy = store.get_model_policy("chief_operating_officer", **SCOPE)
    finance_policy = store.get_model_policy("finance_manager", **SCOPE)
    timestamp = "2026-07-18T12:00:00+00:00"

    conversation = store.save_conversation(AiEmployeeConversation(
        id="conv-order", **SCOPE, employee_id="chief_of_staff", human_owner_id="user-1",
        title="Order", status="active", character_version_id=staff_profile.default_version_id,
        model_policy_id=staff_policy.id,
    ))
    store.save_message(AiEmployeeMessage(
        id="message-z", **SCOPE, conversation_id=conversation.id, speaker_type="human",
        speaker_id="user-1", visibility="shared", content="Question", run_id="run-order",
        created_at=timestamp,
    ))
    store.save_message(AiEmployeeMessage(
        id="message-a", **SCOPE, conversation_id=conversation.id, speaker_type="employee",
        speaker_id="chief_of_staff", visibility="shared", content="Answer", run_id="run-order",
        created_at=timestamp,
    ))
    assert [row.speaker_type for row in store.list_messages(conversation.id, **SCOPE)] == [
        "human", "employee",
    ]

    mission = store.save_mission(AiMission(
        id="mission-order", **SCOPE, goal="Order", sponsor_id="user-1",
        accountable_employee_id="chief_operating_officer", status="draft", phase="scope",
        token_budget=20_000, time_budget_seconds=600, cost_budget_usd=5.0,
    ))
    for employee_id, mission_role, profile, policy, record_id in (
        ("finance_manager", "specialist", finance_profile, finance_policy, "participant-a"),
        ("chief_operating_officer", "accountable", operating_profile, operating_policy, "participant-z"),
        ("chief_of_staff", "orchestrator", staff_profile, staff_policy, "participant-m"),
    ):
        store.save_mission_participant(AiMissionParticipant(
            id=record_id, **SCOPE, mission_id=mission.id, employee_id=employee_id,
            mission_role=mission_role, character_version_id=profile.default_version_id,
            model_policy_id=policy.id, status="active", joined_at=timestamp,
        ))
    assert [row.employee_id for row in store.list_mission_participants(mission.id, **SCOPE)] == [
        "chief_of_staff", "chief_operating_officer", "finance_manager",
    ]


def test_memory_store_persists_exports_and_deletes_the_requested_scope(tmp_path: Path):
    path = tmp_path / "ai-employees.json"
    store = MemoryAiEmployeeStore(str(path))
    store.seed_defaults(**SCOPE, author_id="admin-1")
    profile = store.get_profile("chief_of_staff", **SCOPE)
    policy = store.get_model_policy("chief_of_staff", **SCOPE)
    store.save_conversation(AiEmployeeConversation(
        id="conv-persist", **SCOPE, employee_id="chief_of_staff", human_owner_id="user-1",
        title="Persist me", status="active", character_version_id=profile.default_version_id,
        model_policy_id=policy.id,
    ))

    reopened = MemoryAiEmployeeStore(str(path))
    assert len(reopened.list_profiles(**SCOPE)) == 16
    assert reopened.get_conversation("conv-persist", **SCOPE) is not None
    exported = reopened.export_scope(**SCOPE)
    assert len(exported["profiles"]) == 16
    assert exported["conversations"][0]["id"] == "conv-persist"

    counts = reopened.delete_scope(**SCOPE)
    assert counts["profiles"] == 16
    assert counts["conversations"] == 1
    assert reopened.list_profiles(**SCOPE) == []
    assert reopened.export_scope(**SCOPE)["conversations"] == []


def test_ai_employee_runtime_migration_defines_forced_rls_tables():
    migration = importlib.import_module("migrations.versions.0024_ai_employees_runtime")
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "0024_ai_employees_runtime"
    assert migration.down_revision == "0023_kpi_dashboard_data"
    expected = {
        "ai_employee_profiles",
        "ai_employee_versions",
        "ai_employee_model_policies",
        "ai_employee_conversations",
        "ai_employee_messages",
        "ai_missions",
        "ai_mission_participants",
        "ai_agent_runs",
        "ai_employee_memories",
        "ai_connector_bindings",
        "ai_action_proposals",
    }
    assert set(migration.AI_EMPLOYEE_TABLES) == expected
    assert 'op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")' in source
    assert 'op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")' in source
    for table in expected:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
        assert f"CREATE POLICY onebrain_{table}_scope" in migration._rls_policy_sql(table)

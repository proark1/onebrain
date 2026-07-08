from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

import app.routers.assistant as assistant_router
import app.routers.privacy as privacy_router
import app.routers.service as service_router
from app.assistant.contracts import ASSISTANT_PURPOSES
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.conversations.memory import MemoryConversationStore
from app.intake.memory import MemoryIntakeStore
from app.intake.pipeline import IntakePipeline
from app.platform.base import Account, AppInstallation, Space
from app.platform.memory import MemoryPlatformStore
from app.schemas import AssistantAuditEventCreate, AssistantRecordCreate
from app.security.policy import Classification
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE
from app.store.memory import MemoryStore


def _service_principal(scopes=(SCOPE_READ, SCOPE_WRITE)) -> Principal:
    return Principal(
        user_id="svc:assistant",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,
        locations=frozenset(),
        categories=frozenset({"general"}),
        location_label="-",
        tenant_id="acme",
        principal_type="service",
        scopes=frozenset(scopes),
        account_id="acme",
        app_id="assistant",
        space_ids=frozenset({"sp_business"}),
        purposes=frozenset(ASSISTANT_PURPOSES),
    )


def _admin_principal() -> Principal:
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
    )


def _stores(monkeypatch):
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme GmbH"))
    platform.create_space(Space(id="sp_business", account_id="acme", kind="business", name="Business"))
    platform.install_app(AppInstallation(
        id="appi_assistant",
        account_id="acme",
        app_id="assistant",
        enabled_space_ids=("sp_business",),
        allowed_purposes=tuple(sorted(ASSISTANT_PURPOSES)),
    ))
    intake = MemoryIntakeStore()
    pipeline = IntakePipeline(
        intake,
        type("Settings", (), {"pii_phase": "dpia_signed", "require_approval": False})(),
    )

    monkeypatch.setattr(assistant_router, "_rate_limit", lambda principal: None)
    monkeypatch.setattr(assistant_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(assistant_router, "get_intake_store", lambda: intake)
    monkeypatch.setattr(assistant_router, "get_intake_pipeline", lambda: pipeline)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    return platform, intake


def _create(body: AssistantRecordCreate, monkeypatch):
    platform, intake = _stores(monkeypatch)
    response = assistant_router.create_assistant_record(body, principal=_service_principal())
    fetched = assistant_router.get_assistant_record(response.record.id, principal=_service_principal())
    return response.record, fetched.record, platform, intake


def test_assistant_writes_and_retrieves_core_records(monkeypatch):
    platform, _ = _stores(monkeypatch)
    cases = [
        ("brief", "assistant_briefing", "briefing", "Morning brief: top priority is proposal prep."),
        ("follow_up", "assistant_followup", "follow_up", "Follow up with client about contract review."),
        ("action", "assistant_action", "action_proposal", "Draft a reply and wait for approval."),
        ("voice_transcript", "assistant_voice", "voice_turn", "Voice said: plan my focused work block."),
    ]

    for record_type, purpose, intent, content in cases:
        created = assistant_router.create_assistant_record(
            AssistantRecordCreate(
                content=content,
                title=f"{record_type} sample",
                record_type=record_type,
                intent=intent,
                purpose=purpose,
                space_id="sp_business",
                metadata={"source_system": "assistant-test"},
                provenance={"derived_from": ["sample-fixture"]},
                retention={"policy": "assistant_default"},
            ),
            principal=_service_principal(),
        ).record

        fetched = assistant_router.get_assistant_record(created.id, principal=_service_principal()).record
        assert fetched.content == content
        assert fetched.record_type == record_type
        assert fetched.purpose == purpose
        assert fetched.intent == intent
        assert fetched.metadata["assistant_contract"]["provenance"]["derived_from"] == ["sample-fixture"]

    actions = [event.action for event in platform.list_audit("acme")]
    assert "assistant.record.created" in actions
    assert "assistant.record.read" in actions


def test_assistant_writes_telegram_provider_sync_action_settings_calendar_and_health_records(monkeypatch):
    platform, _ = _stores(monkeypatch)
    cases = [
        ("telegram_binding", "assistant_notification", "Telegram chat tg_123 bound to owner."),
        ("notification_event", "assistant_notification", "Morning brief delivered to Telegram."),
        ("message", "assistant_notification", "Telegram message: what is next today?"),
        ("provider_account", "assistant_connected_account", "Google Workspace account connected."),
        ("scope_grant", "assistant_connected_account", "Granted Gmail readonly and Calendar read scopes."),
        ("secret_reference", "assistant_connected_account", "OAuth refresh secret stored by reference."),
        ("sync_subscription", "assistant_sync", "Gmail watch subscription renewal summary."),
        ("sync_cursor", "assistant_sync", "Gmail history cursor stored behind cursor_ref."),
        ("action_audit", "assistant_action", "Action provenance and source references captured."),
        ("policy_decision", "assistant_action", "Policy requires approval for external send."),
        ("provider_health", "assistant_provider_health", "Microsoft Graph degraded, using reconciliation."),
        ("assistant_setting", "assistant_settings", "Language set to German."),
        ("calendar_focus_plan", "assistant_calendar_planning", "Protected focus block from 09:00 to 11:00."),
        ("model_usage", "assistant_model_usage", "Cheap classifier used for inbox triage."),
        ("security_decision", "assistant_security", "Prompt injection fixture quarantined."),
        ("feedback", "assistant_feedback", "User marked suggestion as good."),
    ]

    for record_type, purpose, content in cases:
        metadata = {"provider": "telegram" if record_type == "message" else "assistant"}
        if record_type == "secret_reference":
            metadata = {"secret_ref": "secret://assistant/acme/google/refresh/v1"}
        if record_type == "sync_cursor":
            metadata = {"cursor_ref": "cursor://assistant/acme/gmail/history"}
        source = "telegram" if record_type == "message" else "assistant"
        created = assistant_router.create_assistant_record(
            AssistantRecordCreate(
                content=content,
                record_type=record_type,
                source=source,
                source_ref=f"{source}:{record_type}:1",
                purpose=purpose,
                space_id="sp_business",
                metadata=metadata,
            ),
            principal=_service_principal(),
        ).record
        fetched = assistant_router.get_assistant_record(created.id, principal=_service_principal()).record
        assert fetched.record_type == record_type
        assert fetched.content == content
        assert fetched.metadata["assistant_contract"]["purpose"] == purpose

    proposed = assistant_router.record_assistant_audit_event(
        AssistantAuditEventCreate(
            action="assistant.action.proposed",
            target_type="action",
            target_id="act_1",
            space_id="sp_business",
            metadata={"risk_tier": "medium"},
        ),
        principal=_service_principal(),
    )
    approved = assistant_router.record_assistant_audit_event(
        AssistantAuditEventCreate(
            action="assistant.action.approved",
            target_type="action",
            target_id="act_1",
            space_id="sp_business",
            decision="approved",
            metadata={"approved_via": "web"},
        ),
        principal=_service_principal(),
    )
    executed = assistant_router.record_assistant_audit_event(
        AssistantAuditEventCreate(
            action="assistant.action.executed",
            target_type="action",
            target_id="act_1",
            space_id="sp_business",
            decision="executed",
            metadata={"idempotency_key_ref": "idem://assistant/acme/act_1"},
        ),
        principal=_service_principal(),
    )

    assert proposed.action == "assistant.action.proposed"
    assert approved.decision == "approved"
    assert executed.meta["idempotency_key_ref"] == "idem://assistant/acme/act_1"
    assert "assistant.action.executed" in [event.action for event in platform.list_audit("acme")]


def test_assistant_lists_scoped_task_and_followup_records(monkeypatch):
    platform, _ = _stores(monkeypatch)
    for record_type, purpose, content in [
        ("task", "assistant_followup", "Prepare the client proposal."),
        ("follow_up", "assistant_followup", "Follow up with client about contract review."),
        ("brief", "assistant_briefing", "Morning brief should not appear in task filter."),
    ]:
        assistant_router.create_assistant_record(
            AssistantRecordCreate(
                content=content,
                record_type=record_type,
                purpose=purpose,
                space_id="sp_business",
            ),
            principal=_service_principal(),
        )

    tasks = assistant_router.list_assistant_records(
        record_type="task",
        intent="",
        account_id="",
        space_id="",
        purpose="",
        status="",
        limit=50,
        principal=_service_principal(),
    )
    followups = assistant_router.list_assistant_records(
        record_type="",
        intent="follow_up",
        account_id="acme",
        space_id="sp_business",
        purpose="assistant_followup",
        status="approved",
        limit=50,
        principal=_service_principal(),
    )

    assert [record.content for record in tasks.records] == ["Prepare the client proposal."]
    assert [record.record_type for record in followups.records] == ["task", "follow_up"]
    assert platform.list_audit("acme")[-1].action == "assistant.records.list"
    assert platform.list_audit("acme")[-1].meta["result_count"] == 2


def test_assistant_record_list_refuses_disallowed_space_filter(monkeypatch):
    _stores(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        assistant_router.list_assistant_records(
            record_type="",
            intent="",
            account_id="acme",
            space_id="sp_other",
            purpose="",
            status="",
            limit=50,
            principal=_service_principal(),
        )

    assert exc.value.status_code == 403
    assert "space" in exc.value.detail


def test_assistant_record_list_does_not_expose_raw_secret_values(monkeypatch):
    _stores(monkeypatch)
    created = assistant_router.create_assistant_record(
        AssistantRecordCreate(
            content="OAuth refresh token stored by reference.",
            record_type="secret_reference",
            purpose="assistant_connected_account",
            space_id="sp_business",
            metadata={"secret_ref": "secret://assistant/acme/google/refresh/v1"},
        ),
        principal=_service_principal(),
    ).record

    response = assistant_router.list_assistant_records(
        record_type="secret_reference",
        intent="",
        account_id="",
        space_id="",
        purpose="assistant_connected_account",
        status="",
        limit=50,
        principal=_service_principal(),
    )

    assert [record.id for record in response.records] == [created.id]
    assert response.records[0].metadata["secret_ref"] == "secret://assistant/acme/google/refresh/v1"
    assert "refresh_token" not in str(response.records[0].metadata)


def test_assistant_secret_refs_export_and_delete_without_raw_secret_values(monkeypatch):
    platform, intake = _stores(monkeypatch)
    conversations = MemoryConversationStore()
    documents = MemoryStore()
    monkeypatch.setattr(privacy_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(privacy_router, "get_intake_store", lambda: intake)
    monkeypatch.setattr(privacy_router, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(privacy_router, "get_store", lambda: documents)

    created = assistant_router.create_assistant_record(
        AssistantRecordCreate(
            content="OAuth refresh token stored in SecretProvider.",
            record_type="secret_reference",
            purpose="assistant_connected_account",
            space_id="sp_business",
            metadata={"secret_ref": "secret://assistant/acme/google/refresh/v1", "provider": "google"},
            retention={"policy": "disconnect_or_account_delete"},
        ),
        principal=_service_principal(),
    ).record

    with pytest.raises(HTTPException) as exc:
        assistant_router.create_assistant_record(
            AssistantRecordCreate(
                content="This should never be stored.",
                record_type="secret_reference",
                purpose="assistant_connected_account",
                space_id="sp_business",
                metadata={"refresh_token": "raw-token-value"},
            ),
            principal=_service_principal(),
        )
    assert exc.value.status_code == 422

    exported = privacy_router.export_account_data("acme", space_id="sp_business", principal=_admin_principal())
    exported_record = next(record for record in exported.intake_records if record["id"] == created.id)
    assert exported_record["metadata"]["secret_ref"] == "secret://assistant/acme/google/refresh/v1"
    assert "raw-token-value" not in str(exported_record)

    erased = privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(
            confirm_account_id="acme",
            space_id="sp_business",
            reason="assistant account deletion test",
        ),
        principal=_admin_principal(),
    )

    assert erased.intake_records_deleted == 1
    assert intake.get(created.id) is None
    assert platform.list_audit("acme")[-1].action == "privacy.erased"


def test_assistant_contracts_do_not_add_canonical_assistant_tables():
    migrations_dir = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    source = "\n".join(path.read_text(encoding="utf-8") for path in migrations_dir.glob("*.py"))

    assert "CREATE TABLE IF NOT EXISTS assistant_" not in source
    assert "op.create_table(\"assistant_" not in source
    assert "op.create_table('assistant_" not in source

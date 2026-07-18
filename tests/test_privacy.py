"""GDPR export/delete operations are account and space scoped."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import HTTPException

import app.routers.privacy as privacy_router
from app.ai_employees.base import AiConnectorBinding
from app.ai_employees.memory import MemoryAiEmployeeStore
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.conversations.base import Scope
from app.conversations.memory import MemoryConversationStore
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.intake.memory import MemoryIntakeStore
from app.intake.pipeline import IntakeInput, IntakePipeline
from app.kpis.base import KpiDefinition, KpiSnapshot
from app.kpis.memory import MemoryKpiStore
from app.platform.base import (
    Account,
    ConsentRecord,
    CredentialMetadata,
    LegalHold,
    Membership,
    Organization,
    RetentionPolicy,
    RetentionRun,
    Space,
)
from app.platform.memory import MemoryPlatformStore
from app.store.memory import MemoryStore


def _principal(role_id: str = "admin") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@operator",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"munich"}),
        categories=role.categories,
        location_label="all",
        tenant_id="nft_gym",
    )


def _fixtures():
    platform = MemoryPlatformStore()
    # The operating admin (user_id="admin@operator") owns this account, so account
    # authorization passes. A different admin is denied — see the cross-account test.
    platform.create_account(Account(id="acme", kind="organization", name="Acme GmbH", owner_user_id="admin@operator"))
    platform.create_space(Space(id="sp_acme_service", account_id="acme", kind="customer_service", name="Service"))
    platform.create_space(Space(id="sp_acme_personal", account_id="acme", kind="personal", name="Owner private"))
    platform.upsert_organization(Organization(id="org_acme", account_id="acme", name="Acme Ops"))
    platform.upsert_membership(Membership(
        id="mem_service",
        account_id="acme",
        user_id="support@acme",
        role_id="admin",
        space_id="sp_acme_service",
        organization_id="org_acme",
    ))
    platform.upsert_consent_record(ConsentRecord(
        id="cons_service",
        account_id="acme",
        space_id="sp_acme_service",
        subject_ref="customer:refund",
        purpose="customer_service_answer",
        status="granted",
    ))
    platform.upsert_retention_policy(RetentionPolicy(
        id="ret_service",
        account_id="acme",
        space_id="sp_acme_service",
        domain="intake",
        record_type="message",
        action="delete",
        duration_days=30,
        legal_basis="customer request",
    ))
    platform.upsert_credential_metadata(CredentialMetadata(
        id="cred_acme",
        account_id="acme",
        provider="google",
        app_id="assistant",
        secret_ref="secret://assistant/acme/google",
    ))

    store = MemoryStore()
    pipe = IngestPipeline(LocalEmbedder(), store)
    service_doc = pipe.ingest_text(
        title="Service transcript",
        text="Customer asked about refund timing.",
        classification="internal",
        location="global",
        category="general",
        uploaded_by="svc:communication",
        tenant="acme",
        account_id="acme",
        space_id="sp_acme_service",
    )
    personal_doc = pipe.ingest_text(
        title="Owner note",
        text="Private owner family reminder.",
        classification="restricted",
        location="global",
        category="general",
        uploaded_by="owner@acme",
        tenant="acme",
        account_id="acme",
        space_id="sp_acme_personal",
    )

    conversations = MemoryConversationStore()
    service_conv = conversations.create(Scope("acme", "support@acme", "admin", "acme", "sp_acme_service"), "Refund")
    conversations.add_message(service_conv.id, "user", "When is my refund paid?")
    conversations.add_message(service_conv.id, "assistant", "Refunds take five days.")
    personal_conv = conversations.create(Scope("acme", "owner@acme", "admin", "acme", "sp_acme_personal"), "Family")
    conversations.add_message(personal_conv.id, "user", "Family dinner reminder")

    intake = MemoryIntakeStore()
    intake_pipe = IntakePipeline(
        intake,
        type("Settings", (), {"pii_phase": "dpia_signed", "require_approval": False})(),
    )
    service_record = intake_pipe.ingest(IntakeInput(
        tenant_id="acme",
        account_id="acme",
        space_id="sp_acme_service",
        app_id="communication",
        purpose="customer_service_inbox",
        source="communication",
        content="Customer asked about refund timing.",
    ))
    personal_record = intake_pipe.ingest(IntakeInput(
        tenant_id="acme",
        account_id="acme",
        space_id="sp_acme_personal",
        app_id="assistant",
        purpose="assistant_action",
        source="assistant",
        content="Private owner family reminder.",
    ))

    return (
        platform, store, conversations, intake, service_doc, personal_doc,
        service_conv, personal_conv, service_record, personal_record,
    )


class _ConnectorCleanup:
    def __init__(self, deleted: int = 0):
        self.deleted = deleted
        self.calls = []

    def purge_local_credentials(self, **kwargs):
        self.calls.append(kwargs)
        return self.deleted


def _patch(
    monkeypatch,
    platform,
    store,
    conversations,
    intake,
    kpis=None,
    ai_employees=None,
    connector=None,
):
    kpis = kpis or MemoryKpiStore()
    ai_employees = ai_employees or MemoryAiEmployeeStore()
    connector = connector or _ConnectorCleanup()
    monkeypatch.setattr(privacy_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(privacy_router, "get_store", lambda: store)
    monkeypatch.setattr(privacy_router, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(privacy_router, "get_intake_store", lambda: intake)
    monkeypatch.setattr(privacy_router, "get_kpi_store", lambda: kpis)
    monkeypatch.setattr(privacy_router, "get_ai_employee_store", lambda: ai_employees)
    monkeypatch.setattr(
        privacy_router,
        "get_ai_employee_google_calendar_connector",
        lambda: connector,
    )
    return kpis


def test_privacy_export_is_space_scoped_and_audited(monkeypatch):
    platform, store, conversations, intake, service_doc, _, service_conv, _, service_record, _ = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    exported = privacy_router.export_account_data(
        "acme",
        space_id="sp_acme_service",
        principal=_principal("admin"),
    )

    assert exported.account_id == "acme"
    assert exported.space_id == "sp_acme_service"
    assert [doc["doc_id"] for doc in exported.documents] == [service_doc.doc_id]
    assert exported.documents[0]["chunks"][0]["text"] == "Customer asked about refund timing."
    assert [conversation["id"] for conversation in exported.conversations] == [service_conv.id]
    assert exported.conversations[0]["messages"][0]["content"] == "When is my refund paid?"
    assert [record["id"] for record in exported.intake_records] == [service_record.id]
    assert [row["id"] for row in exported.governance["memberships"]] == ["mem_service"]
    assert [row["id"] for row in exported.governance["consent_records"]] == ["cons_service"]
    assert [row["id"] for row in exported.governance["retention_policies"]] == ["ret_service"]
    assert exported.governance["credential_metadata"] == []
    assert platform.list_audit("acme")[-1].action == "privacy.exported"
    assert platform.list_audit("acme")[-1].meta["documents"] == 1
    assert platform.list_audit("acme")[-1].meta["intake_records"] == 1


def test_privacy_export_and_erase_cover_ai_employees_and_connector_secrets(monkeypatch):
    platform, store, conversations, intake, *_ = _fixtures()
    employees = MemoryAiEmployeeStore()
    employees.seed_defaults(
        tenant_id="acme",
        account_id="acme",
        space_id="sp_acme_service",
        author_id="system:test",
    )
    binding = employees.save_connector_binding(AiConnectorBinding(
        id="binding_service",
        tenant_id="acme",
        account_id="acme",
        space_id="sp_acme_service",
        provider="google_calendar",
        credential_ref="secret://ai-employees/google-calendar/acme/credential",
        resource_type="calendar",
        resource_ids=("primary",),
        employee_ids=("chief_of_staff",),
        capabilities=("calendar_read",),
        status="active",
    ))
    connector = _ConnectorCleanup(deleted=1)
    _patch(
        monkeypatch,
        platform,
        store,
        conversations,
        intake,
        ai_employees=employees,
        connector=connector,
    )

    exported = privacy_router.export_account_data(
        "acme",
        space_id="sp_acme_service",
        principal=_principal("admin"),
    )
    assert len(exported.ai_employees["profiles"]) == 16
    assert exported.ai_employees["connector_bindings"][0]["credential_ref"] == "secret://redacted"

    erased = privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(
            confirm_account_id="acme",
            space_id="sp_acme_service",
            reason="data subject request",
        ),
        principal=_principal("admin"),
    )
    assert erased.ai_employees_deleted["profiles"] == 16
    assert erased.ai_employees_deleted["connector_bindings"] == 1
    assert erased.connector_credentials_deleted == 1
    assert connector.calls[0]["bindings"] == (binding,)
    assert employees.export_scope(
        tenant_id="acme", account_id="acme", space_id="sp_acme_service",
    )["profiles"] == []


def test_privacy_erase_requires_confirmation_and_deletes_only_scope(monkeypatch):
    (
        platform, store, conversations, intake, service_doc, personal_doc,
        service_conv, personal_conv, service_record, personal_record,
    ) = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    with pytest.raises(HTTPException) as exc:
        privacy_router.erase_account_data(
            "acme",
            privacy_router.PrivacyEraseRequest(confirm_account_id="wrong", space_id="sp_acme_service"),
            principal=_principal("admin"),
        )
    assert exc.value.status_code == 400

    erased = privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(
            confirm_account_id="acme",
            space_id="sp_acme_service",
            reason="customer requested deletion",
        ),
        principal=_principal("admin"),
    )

    assert erased.documents_deleted == 1
    assert erased.chunks_deleted == 1
    assert erased.conversations_deleted == 1
    assert erased.intake_records_deleted == 1
    assert erased.governance_deleted["memberships"] == 1
    assert erased.governance_deleted["consent_records"] == 1
    assert erased.governance_deleted["retention_policies"] == 1
    assert store.get_document_meta(service_doc.doc_id) is None
    assert store.get_document_meta(personal_doc.doc_id) is not None
    assert conversations.export_scope("acme", account_id="acme", space_id="sp_acme_service") == []
    assert conversations.export_scope("acme", account_id="acme", space_id="sp_acme_personal")[0]["id"] == personal_conv.id
    assert intake.get(service_record.id) is None
    assert intake.get(personal_record.id) is not None
    assert platform.list_organizations("acme")[0].id == "org_acme"
    assert [row.id for row in platform.list_memberships("acme")] == []
    assert platform.list_credential_metadata("acme")[0].id == "cred_acme"
    audit = platform.list_audit("acme")[-1]
    assert audit.action == "privacy.erased"
    assert audit.meta["reason"] == "customer requested deletion"
    assert audit.meta["intake_records_deleted"] == 1


def test_privacy_export_and_erase_include_only_matching_kpi_scope(monkeypatch):
    platform, store, conversations, intake, *_ = _fixtures()
    kpis = MemoryKpiStore()
    _patch(monkeypatch, platform, store, conversations, intake, kpis)
    service_definition = kpis.create_definition(KpiDefinition(
        id="kpi_service",
        account_id="acme",
        space_id="sp_acme_service",
        key="sla_health",
        name="SLA health",
    ))
    kpis.create_definition(KpiDefinition(
        id="kpi_personal",
        account_id="acme",
        space_id="sp_acme_personal",
        key="private_metric",
        name="Private metric",
    ))
    kpis.ingest_snapshots([KpiSnapshot(
        id="snap_service",
        account_id="acme",
        space_id="sp_acme_service",
        kpi_id=service_definition.id,
        value=Decimal("97"),
        observed_at="2026-07-15T09:00:00+00:00",
        received_at="2026-07-15T09:01:00+00:00",
        source_ref="support-summary",
        idempotency_key="sla-health-1",
        created_by="svc:kpi",
    )])

    exported = privacy_router.export_account_data(
        "acme", space_id="sp_acme_service", principal=_principal("admin"),
    )
    assert [row["id"] for row in exported.kpis["definitions"]] == ["kpi_service"]
    assert [row["id"] for row in exported.kpis["snapshots"]] == ["snap_service"]

    erased = privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(
            confirm_account_id="acme", space_id="sp_acme_service", reason="scope erasure",
        ),
        principal=_principal("admin"),
    )
    assert erased.kpis_deleted == {"definitions": 1, "snapshots": 1}
    assert kpis.export_scope("acme", "sp_acme_service")["definitions"] == []
    assert [row["id"] for row in kpis.export_scope("acme", "sp_acme_personal")["definitions"]] == [
        "kpi_personal",
    ]


def test_privacy_operations_require_admin_and_valid_scope(monkeypatch):
    platform, store, conversations, intake, *_ = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    with pytest.raises(HTTPException) as exc:
        privacy_router.export_account_data("acme", principal=_principal("front_desk"))
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        privacy_router.export_account_data("missing", principal=_principal("admin"))
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as wrong_space:
        privacy_router.export_account_data("acme", space_id="sp_other", principal=_principal("admin"))
    assert wrong_space.value.status_code == 404


def test_privacy_cross_account_admin_is_denied(monkeypatch):
    """An admin who neither owns nor has an admin membership in the account is
    refused — and gets the same 404 as a missing account, so existence can't be
    probed. This is the cross-account boundary (e.g. nft_gym admin vs Communication)."""
    (
        platform, store, conversations, intake, service_doc, _,
        _, _, service_record, _,
    ) = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)
    outsider = replace(_principal("admin"), user_id="admin@other-account")

    with pytest.raises(HTTPException) as export_denied:
        privacy_router.export_account_data("acme", principal=outsider)
    assert export_denied.value.status_code == 404

    with pytest.raises(HTTPException) as erase_denied:
        privacy_router.erase_account_data(
            "acme",
            privacy_router.PrivacyEraseRequest(confirm_account_id="acme"),
            principal=outsider,
        )
    assert erase_denied.value.status_code == 404
    # Authorization ran before any deletion: the data is untouched despite a
    # valid confirm_account_id.
    assert store.get_document_meta(service_doc.doc_id) is not None
    assert intake.get(service_record.id) is not None


def test_erase_emits_a_tombstone_for_modules(monkeypatch):
    platform, store, conversations, intake, *_rest = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(
            confirm_account_id="acme", space_id="sp_acme_service", reason="gdpr erasure",
        ),
        principal=_principal("admin"),
    )

    tombstones = platform.list_tombstones("acme")
    assert len(tombstones) == 1
    assert tombstones[0].target_type == "space"
    assert tombstones[0].space_id == "sp_acme_service"
    assert tombstones[0].reason == "gdpr erasure"
    assert tombstones[0].seq >= 1


def test_legal_hold_blocks_then_release_unblocks_erase(monkeypatch):
    platform, store, conversations, intake, service_doc, *_rest = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    hold = privacy_router.create_account_legal_hold(
        "acme", privacy_router.LegalHoldCreate(reason="litigation matter 42"), principal=_principal("admin"),
    )
    assert hold.active is True

    # Erasing a held scope is refused (409) and nothing is deleted; the refusal is audited.
    with pytest.raises(HTTPException) as exc:
        privacy_router.erase_account_data(
            "acme",
            privacy_router.PrivacyEraseRequest(confirm_account_id="acme", space_id="sp_acme_service"),
            principal=_principal("admin"),
        )
    assert exc.value.status_code == 409
    assert store.get_document_meta(service_doc.doc_id) is not None
    denial = platform.list_audit("acme")[-1]
    assert denial.action == "privacy.erase_denied" and denial.decision == "denied_legal_hold"

    # Release the hold, and the same erase now succeeds.
    released = privacy_router.release_account_legal_hold("acme", hold.id, principal=_principal("admin"))
    assert released.active is False
    erased = privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(confirm_account_id="acme", space_id="sp_acme_service"),
        principal=_principal("admin"),
    )
    assert erased.documents_deleted == 1
    assert store.get_document_meta(service_doc.doc_id) is None


def test_space_hold_blocks_only_its_scope(monkeypatch):
    platform, store, conversations, intake, service_doc, *_rest = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    # Hold only the personal space.
    privacy_router.create_account_legal_hold(
        "acme",
        privacy_router.LegalHoldCreate(space_id="sp_acme_personal", reason="preserve owner data"),
        principal=_principal("admin"),
    )

    # Erasing a different space still works...
    erased = privacy_router.erase_account_data(
        "acme",
        privacy_router.PrivacyEraseRequest(confirm_account_id="acme", space_id="sp_acme_service"),
        principal=_principal("admin"),
    )
    assert erased.documents_deleted == 1

    # ...but an account-wide erase is blocked because a space within it is held.
    with pytest.raises(HTTPException) as exc:
        privacy_router.erase_account_data(
            "acme",
            privacy_router.PrivacyEraseRequest(confirm_account_id="acme"),
            principal=_principal("admin"),
        )
    assert exc.value.status_code == 409


def test_legal_hold_list_and_release_lifecycle(monkeypatch):
    platform, store, conversations, intake, *_ = _fixtures()
    _patch(monkeypatch, platform, store, conversations, intake)

    hold = privacy_router.create_account_legal_hold(
        "acme", privacy_router.LegalHoldCreate(reason="r"), principal=_principal("admin"),
    )
    active = privacy_router.list_account_legal_holds("acme", principal=_principal("admin"))
    assert [h.id for h in active] == [hold.id]

    privacy_router.release_account_legal_hold("acme", hold.id, principal=_principal("admin"))
    assert privacy_router.list_account_legal_holds("acme", principal=_principal("admin")) == []
    with_released = privacy_router.list_account_legal_holds("acme", include_released=True, principal=_principal("admin"))
    assert [h.id for h in with_released] == [hold.id]

    with pytest.raises(HTTPException) as exc:
        privacy_router.release_account_legal_hold("acme", "nonexistent", principal=_principal("admin"))
    assert exc.value.status_code == 404


def test_retention_skips_held_scope_and_records_run(monkeypatch):
    import app.deps as deps
    from app.retention.service import run_retention

    platform, store, conversations, intake, _sd, _pd, _sc, _pc, service_record, _pr = _fixtures()
    monkeypatch.setattr(deps, "get_platform_store", lambda: platform)
    monkeypatch.setattr(deps, "get_store", lambda: store)
    monkeypatch.setattr(deps, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(deps, "get_intake_store", lambda: intake)

    # _fixtures() seeds an active intake retention policy (30 days) on
    # sp_acme_service. Backdate the record so it is past that window.
    intake._records[service_record.id] = replace(
        intake.get(service_record.id), created_at="2020-01-01T00:00:00+00:00",
    )
    platform.create_legal_hold(LegalHold(
        id="h_ret", account_id="acme", space_id="sp_acme_service", reason="hold service data",
    ))
    held_run = run_retention(account_id="acme", space_id="sp_acme_service", dry_run=False)
    assert held_run["legal_hold"] is True
    assert intake.get(service_record.id) is not None            # not deleted under hold
    assert platform.list_retention_runs("acme")[-1].status == "skipped_legal_hold"

    # After release, the sweep deletes and records a completed run.
    platform.release_legal_hold("acme", "h_ret")
    done_run = run_retention(account_id="acme", space_id="sp_acme_service", dry_run=False)
    assert done_run["legal_hold"] is False
    assert intake.get(service_record.id) is None
    runs = platform.list_retention_runs("acme")
    assert runs[-1].status == "completed"
    assert runs[-2].created_at < runs[-1].created_at


def test_retention_run_listing_preserves_recording_order_when_timestamps_tie():
    platform, *_ = _fixtures()
    at = "2026-07-18T08:00:00+00:00"
    platform.record_retention_run(RetentionRun(
        id="ret_z_first", account_id="acme", space_id="sp_acme_service",
        created_at=at, completed_at=at,
    ))
    platform.record_retention_run(RetentionRun(
        id="ret_a_second", account_id="acme", space_id="sp_acme_service",
        created_at=at, completed_at=at,
    ))

    assert [run.id for run in platform.list_retention_runs("acme")] == [
        "ret_z_first",
        "ret_a_second",
    ]


def test_postgres_retention_run_listing_breaks_created_at_ties_by_completion_time():
    from app.platform.postgres import PostgresPlatformStore

    store = object.__new__(PostgresPlatformStore)
    called = {}

    def list_scope(table, columns, account_id, space_id, order):
        called["order"] = order
        return []

    store._list_scope = list_scope

    assert store.list_retention_runs("acme") == []
    assert called["order"] == "created_at, completed_at, id"


def test_retention_deletes_only_records_older_than_duration(monkeypatch):
    import app.deps as deps
    from app.retention.service import run_retention

    platform, store, conversations, intake, _sd, _pd, _sc, _pc, service_record, _pr = _fixtures()
    monkeypatch.setattr(deps, "get_platform_store", lambda: platform)
    monkeypatch.setattr(deps, "get_store", lambda: store)
    monkeypatch.setattr(deps, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(deps, "get_intake_store", lambda: intake)

    # A second intake record in the same scope, this one recent. The seeded one is
    # backdated past the 30-day policy.
    from app.intake.pipeline import IntakeInput, IntakePipeline
    intake_pipe = IntakePipeline(
        intake, type("Settings", (), {"pii_phase": "dpia_signed", "require_approval": False})(),
    )
    recent = intake_pipe.ingest(IntakeInput(
        tenant_id="acme", account_id="acme", space_id="sp_acme_service",
        app_id="communication", purpose="customer_service_inbox", source="communication",
        content="A very recent customer message.",
    ))
    intake._records[service_record.id] = replace(
        intake.get(service_record.id), created_at="2020-01-01T00:00:00+00:00",
    )

    # Dry run counts only the aged record; nothing is deleted.
    preview = run_retention(account_id="acme", space_id="sp_acme_service", dry_run=True)
    assert preview["counts"]["intake_records"] == 1
    assert intake.get(service_record.id) is not None and intake.get(recent.id) is not None

    # Real run deletes the aged record and keeps the recent one.
    run_retention(account_id="acme", space_id="sp_acme_service", dry_run=False)
    assert intake.get(service_record.id) is None
    assert intake.get(recent.id) is not None


def test_kpi_retention_uses_received_time_and_preserves_definitions(monkeypatch):
    import app.deps as deps
    from app.retention.service import run_retention

    platform, store, conversations, intake, *_ = _fixtures()
    kpis = MemoryKpiStore()
    monkeypatch.setattr(deps, "get_platform_store", lambda: platform)
    monkeypatch.setattr(deps, "get_store", lambda: store)
    monkeypatch.setattr(deps, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(deps, "get_intake_store", lambda: intake)
    monkeypatch.setattr(deps, "get_kpi_store", lambda: kpis)
    platform.upsert_retention_policy(RetentionPolicy(
        id="ret_kpis",
        account_id="acme",
        space_id="sp_acme_service",
        domain="kpis",
        record_type="snapshot",
        action="delete",
        duration_days=30,
        legal_basis="business retention policy",
    ))
    definition = kpis.create_definition(KpiDefinition(
        id="kpi_retention",
        account_id="acme",
        space_id="sp_acme_service",
        key="sla_health",
        name="SLA health",
    ))
    kpis.ingest_snapshots([
        KpiSnapshot(
            id="snap_old",
            account_id="acme",
            space_id="sp_acme_service",
            kpi_id=definition.id,
            value=Decimal("90"),
            observed_at="2026-07-15T09:00:00+00:00",
            received_at="2020-01-01T00:00:00+00:00",
            source_ref="",
            idempotency_key="old",
            created_by="svc:kpi",
        ),
        KpiSnapshot(
            id="snap_recent",
            account_id="acme",
            space_id="sp_acme_service",
            kpi_id=definition.id,
            value=Decimal("97"),
            # An old observation must not age out a recently received snapshot.
            observed_at="2020-01-01T00:00:00+00:00",
            received_at="2026-07-15T09:01:00+00:00",
            source_ref="",
            idempotency_key="recent",
            created_by="svc:kpi",
        ),
    ])

    preview = run_retention(
        account_id="acme", space_id="sp_acme_service", domain="kpis", dry_run=True,
    )
    assert preview["counts"]["kpis"]["snapshots"] == 1
    completed = run_retention(
        account_id="acme", space_id="sp_acme_service", domain="kpis", dry_run=False,
    )
    assert completed["counts"]["kpis"]["snapshots_deleted"] == 1
    assert [row.id for row in kpis.list_definitions("acme", "sp_acme_service")] == [definition.id]
    assert [row.id for row in kpis.list_snapshots(
        definition.id, account_id="acme", space_id="sp_acme_service", limit=30,
    )] == ["snap_recent"]

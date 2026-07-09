"""GDPR export/delete operations are account and space scoped."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.privacy as privacy_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.conversations.base import Scope
from app.conversations.memory import MemoryConversationStore
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.intake.memory import MemoryIntakeStore
from app.intake.pipeline import IntakeInput, IntakePipeline
from app.platform.base import Account, ConsentRecord, CredentialMetadata, Membership, Organization, RetentionPolicy, Space
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
    platform.create_account(Account(id="acme", kind="organization", name="Acme GmbH"))
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


def _patch(monkeypatch, platform, store, conversations, intake):
    monkeypatch.setattr(privacy_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(privacy_router, "get_store", lambda: store)
    monkeypatch.setattr(privacy_router, "get_conversation_store", lambda: conversations)
    monkeypatch.setattr(privacy_router, "get_intake_store", lambda: intake)


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

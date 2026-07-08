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
from app.platform.base import Account, Space
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

    return platform, store, conversations, service_doc, personal_doc, service_conv, personal_conv


def _patch(monkeypatch, platform, store, conversations):
    monkeypatch.setattr(privacy_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(privacy_router, "get_store", lambda: store)
    monkeypatch.setattr(privacy_router, "get_conversation_store", lambda: conversations)


def test_privacy_export_is_space_scoped_and_audited(monkeypatch):
    platform, store, conversations, service_doc, _, service_conv, _ = _fixtures()
    _patch(monkeypatch, platform, store, conversations)

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
    assert platform.list_audit("acme")[-1].action == "privacy.exported"
    assert platform.list_audit("acme")[-1].meta["documents"] == 1


def test_privacy_erase_requires_confirmation_and_deletes_only_scope(monkeypatch):
    platform, store, conversations, service_doc, personal_doc, service_conv, personal_conv = _fixtures()
    _patch(monkeypatch, platform, store, conversations)

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
    assert store.get_document_meta(service_doc.doc_id) is None
    assert store.get_document_meta(personal_doc.doc_id) is not None
    assert conversations.export_scope("acme", account_id="acme", space_id="sp_acme_service") == []
    assert conversations.export_scope("acme", account_id="acme", space_id="sp_acme_personal")[0]["id"] == personal_conv.id
    audit = platform.list_audit("acme")[-1]
    assert audit.action == "privacy.erased"
    assert audit.meta["reason"] == "customer requested deletion"


def test_privacy_operations_require_admin_and_valid_scope(monkeypatch):
    platform, store, conversations, *_ = _fixtures()
    _patch(monkeypatch, platform, store, conversations)

    with pytest.raises(HTTPException) as exc:
        privacy_router.export_account_data("acme", principal=_principal("front_desk"))
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        privacy_router.export_account_data("missing", principal=_principal("admin"))
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as wrong_space:
        privacy_router.export_account_data("acme", space_id="sp_other", principal=_principal("admin"))
    assert wrong_space.value.status_code == 404

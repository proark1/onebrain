"""Human document management with OneBrain platform spaces."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.documents as documents_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.platform.base import Account, Space
from app.platform.memory import MemoryPlatformStore
from app.store.memory import MemoryStore


def _principal(role_id: str = "admin", *, tenant: str = "nft_gym", user_id: str | None = None) -> Principal:
    role = ROLES[role_id]
    if role.scope == "chain":
        locations = None
    elif role.scope == "location":
        locations = frozenset({"munich"})
    else:
        locations = frozenset()
    return Principal(
        user_id=user_id or f"{role_id}@nft_gym",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=locations,
        categories=role.categories,
        location_label="munich",
        tenant_id=tenant,
    )


def _platform_store() -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="nft_gym", kind="organization", name="NFT Gym"))
    platform.create_space(Space(id="sp_customer", account_id="nft_gym", kind="customer_service", name="Customer service"))
    platform.create_space(Space(id="sp_personal", account_id="nft_gym", kind="personal", name="Owner private"))
    return platform


def _store_with_docs(*, require_approval: bool = False):
    store = MemoryStore()
    pipe = IngestPipeline(LocalEmbedder(), store)
    customer = pipe.ingest_text(
        title="Customer refund policy",
        text="Customer refunds are handled by the support team.",
        classification="public",
        location="global",
        category="general",
        uploaded_by="uploader",
        tenant="nft_gym",
        require_approval=require_approval,
        account_id="nft_gym",
        space_id="sp_customer",
    )
    personal = pipe.ingest_text(
        title="Owner personal note",
        text="Owner private calendar preference.",
        classification="public",
        location="global",
        category="general",
        uploaded_by="uploader",
        tenant="nft_gym",
        require_approval=require_approval,
        account_id="nft_gym",
        space_id="sp_personal",
    )
    return store, customer, personal


def _patch_stores(monkeypatch, store: MemoryStore, platform: MemoryPlatformStore):
    monkeypatch.setattr(documents_router, "get_store", lambda: store)
    monkeypatch.setattr(documents_router, "get_platform_store", lambda: platform)


def test_scoped_document_list_only_returns_requested_space(monkeypatch):
    store, _, _ = _store_with_docs()
    _patch_stores(monkeypatch, store, _platform_store())

    docs = documents_router.list_documents(
        account_id="nft_gym",
        space_id="sp_customer",
        principal=_principal("admin"),
    )

    assert [d.title for d in docs] == ["Customer refund policy"]
    assert docs[0].account_id == "nft_gym"
    assert docs[0].space_id == "sp_customer"


def test_scoped_document_operations_require_admin(monkeypatch):
    store, _, _ = _store_with_docs()
    _patch_stores(monkeypatch, store, _platform_store())

    with pytest.raises(HTTPException) as exc:
        documents_router.list_documents(
            account_id="nft_gym",
            space_id="sp_customer",
            principal=_principal("front_desk"),
        )

    assert exc.value.status_code == 403


def test_pending_review_is_space_scoped_and_approval_checks_space(monkeypatch):
    store, customer, personal = _store_with_docs(require_approval=True)
    _patch_stores(monkeypatch, store, _platform_store())
    admin = _principal("admin", user_id="reviewer@nft_gym")

    pending = documents_router.list_pending(
        account_id="nft_gym",
        space_id="sp_customer",
        principal=admin,
    )
    assert [d.title for d in pending] == ["Customer refund policy"]

    with pytest.raises(HTTPException) as exc:
        documents_router.approve_document(
            personal.doc_id,
            account_id="nft_gym",
            space_id="sp_customer",
            principal=admin,
        )
    assert exc.value.status_code == 404
    assert store.get_document_meta(personal.doc_id)["status"] == "pending"

    approved = documents_router.approve_document(
        customer.doc_id,
        account_id="nft_gym",
        space_id="sp_customer",
        principal=admin,
    )
    assert approved["approved"] == customer.doc_id
    assert store.get_document_meta(customer.doc_id)["status"] == "approved"


def test_delete_document_checks_tenant_and_space_before_removal(monkeypatch):
    store, customer, personal = _store_with_docs()
    _patch_stores(monkeypatch, store, _platform_store())
    admin = _principal("admin")

    with pytest.raises(HTTPException) as exc:
        documents_router.delete_document(
            personal.doc_id,
            account_id="nft_gym",
            space_id="sp_customer",
            principal=admin,
        )
    assert exc.value.status_code == 404
    assert store.get_document_meta(personal.doc_id) is not None

    deleted = documents_router.delete_document(
        customer.doc_id,
        account_id="nft_gym",
        space_id="sp_customer",
        principal=admin,
    )
    assert deleted["deleted"] == customer.doc_id
    assert store.get_document_meta(customer.doc_id) is None


def test_delete_document_refuses_other_tenant_even_without_space_scope(monkeypatch):
    store = MemoryStore()
    pipe = IngestPipeline(LocalEmbedder(), store)
    other = pipe.ingest_text(
        title="Company B document",
        text="Private document for another tenant.",
        classification="public",
        location="global",
        category="general",
        uploaded_by="uploader",
        tenant="companyB",
    )
    _patch_stores(monkeypatch, store, _platform_store())

    with pytest.raises(HTTPException) as exc:
        documents_router.delete_document(other.doc_id, principal=_principal("admin"))

    assert exc.value.status_code == 404
    assert store.get_document_meta(other.doc_id) is not None

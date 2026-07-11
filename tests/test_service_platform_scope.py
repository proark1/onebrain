"""Service API + platform space scoping.

This is the first bridge between the old service-key surface and the new
OneBrain platform model: app calls can be scoped to an account/space/purpose,
and retrieval is then narrowed to that exact space.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

import app.routers.service as service_router
from app.auth.principal import Principal
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.intake.memory import MemoryIntakeStore
from app.intake.pipeline import IntakePipeline
from app.llm.local import LocalLLM
from app.platform.base import Account, AppInstallation, LegalHold, Space, Tombstone
from app.platform.memory import MemoryPlatformStore
from app.retrieval.service import RetrievalService
from app.schemas import ServiceAskRequest, ServiceCaptureRequest
from app.schemas import ServiceIntakeRequest
from app.security.policy import Classification
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE
from app.store.memory import MemoryStore


def _svc_principal(scopes=(SCOPE_READ,), tenant="nft_gym"):
    return Principal(
        user_id="svc:key",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,
        locations=frozenset(),
        categories=frozenset({"general"}),
        location_label="-",
        tenant_id=tenant,
        principal_type="service",
        scopes=frozenset(scopes),
    )


def _platform_store() -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="nft_gym", kind="organization", name="NFT Gym"))
    platform.create_space(Space(id="sp_customer", account_id="nft_gym", kind="customer_service", name="Customer service"))
    platform.create_space(Space(id="sp_personal", account_id="nft_gym", kind="personal", name="Owner private"))
    platform.install_app(AppInstallation(
        id="appi_comm",
        account_id="nft_gym",
        app_id="communication",
        enabled_space_ids=("sp_customer", "sp_personal"),
        allowed_purposes=("customer_service_answer", "customer_service_inbox"),
    ))
    return platform


def _knowledge_service():
    emb = LocalEmbedder()
    store = MemoryStore()
    pipe = IngestPipeline(emb, store)
    pipe.ingest_text(
        title="Customer service hours",
        text="Customer support hours are Monday to Friday from 09:00 to 18:00.",
        classification="public",
        location="global",
        category="general",
        uploaded_by="seed",
        tenant="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
    )
    pipe.ingest_text(
        title="Owner private preferences",
        text="Private owner note: Friday calls should be avoided.",
        classification="public",
        location="global",
        category="general",
        uploaded_by="seed",
        tenant="nft_gym",
        account_id="nft_gym",
        space_id="sp_personal",
    )
    return store, pipe, RetrievalService(emb, store, LocalLLM(), top_k=8)


def test_service_ask_is_scoped_to_enabled_customer_service_space(monkeypatch):
    _, _, retrieval = _knowledge_service()
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_retrieval_service", lambda: retrieval)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)

    response = service_router.service_ask(
        ServiceAskRequest(
            question="What are the customer support hours?",
            account_id="nft_gym",
            space_id="sp_customer",
            app_id="communication",
        ),
        principal=_svc_principal(),
    )

    assert response.chunks_used >= 1
    assert "09:00" in response.answer
    assert "Friday calls should be avoided" not in response.answer
    audit = platform.list_audit("nft_gym")
    assert audit and audit[-1].decision == "allowed"


def test_service_ask_denies_customer_service_access_to_personal_space(monkeypatch):
    _, _, retrieval = _knowledge_service()
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_retrieval_service", lambda: retrieval)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)

    with pytest.raises(HTTPException) as exc:
        service_router.service_ask(
            ServiceAskRequest(
                question="What private owner notes exist?",
                account_id="nft_gym",
                space_id="sp_personal",
                app_id="communication",
            ),
            principal=_svc_principal(),
        )

    assert exc.value.status_code == 403
    assert "customer_service_cannot_use_private_space" in exc.value.detail
    assert platform.list_audit("nft_gym")[-1].decision == "denied"


def test_service_capture_stamps_platform_space_metadata(monkeypatch):
    store = MemoryStore()
    pipe = IngestPipeline(LocalEmbedder(), store)
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_pipeline", lambda: pipe)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)

    result = service_router.capture(
        ServiceCaptureRequest(
            text="A customer asked a synthetic question about memberships.",
            title="widget capture",
            account_id="nft_gym",
            space_id="sp_customer",
            app_id="communication",
        ),
        principal=_svc_principal(scopes=(SCOPE_WRITE,)),
    )

    assert result["chunks"] == 1
    assert store._chunks[0].meta["account_id"] == "nft_gym"
    assert store._chunks[0].meta["space_id"] == "sp_customer"
    assert store._chunks[0].meta["tenant_id"] == "nft_gym"


def test_constrained_service_key_cannot_switch_app_space_or_purpose(monkeypatch):
    _, _, retrieval = _knowledge_service()
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_retrieval_service", lambda: retrieval)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    principal = replace(
        _svc_principal(scopes=(SCOPE_READ,), tenant="nft_gym"),
        account_id="nft_gym",
        app_id="communication",
        space_ids=frozenset({"sp_customer"}),
        purposes=frozenset({"customer_service_answer"}),
    )

    response = service_router.service_ask(
        ServiceAskRequest(question="What are the customer support hours?", space_id="sp_customer"),
        principal=principal,
    )
    assert response.chunks_used >= 1

    attempts = [
        ServiceAskRequest(
            question="Can I pretend to be the assistant?",
            account_id="nft_gym",
            space_id="sp_customer",
            app_id="assistant",
        ),
        ServiceAskRequest(
            question="Can I use the private space?",
            account_id="nft_gym",
            space_id="sp_personal",
            app_id="communication",
        ),
        ServiceAskRequest(
            question="Can I use an inbox purpose?",
            account_id="nft_gym",
            space_id="sp_customer",
            app_id="communication",
            purpose="customer_service_inbox",
        ),
    ]

    for request in attempts:
        with pytest.raises(HTTPException) as exc:
            service_router.service_ask(request, principal=principal)
        assert exc.value.status_code == 403


def test_service_capabilities_exposes_key_scope_without_secret():
    principal = replace(
        _svc_principal(scopes=(SCOPE_READ, SCOPE_WRITE), tenant="nft_gym"),
        account_id="nft_gym",
        app_id="communication",
        space_ids=frozenset({"sp_customer"}),
        purposes=frozenset({"customer_service_answer", "customer_service_inbox"}),
    )

    capabilities = service_router.capabilities(principal=principal)

    assert capabilities.tenant_id == "nft_gym"
    assert capabilities.account_id == "nft_gym"
    assert capabilities.app_id == "communication"
    assert capabilities.space_ids == ["sp_customer"]
    assert capabilities.purposes == ["customer_service_answer", "customer_service_inbox"]
    assert set(capabilities.scopes) == {SCOPE_READ, SCOPE_WRITE}
    assert not hasattr(capabilities, "key")


def test_service_brand_theme_can_be_read_and_overridden_by_app_key(monkeypatch):
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    principal = replace(
        _svc_principal(scopes=(SCOPE_READ, SCOPE_WRITE), tenant="nft_gym"),
        account_id="nft_gym",
        app_id="communication",
        space_ids=frozenset({"sp_customer"}),
        purposes=frozenset({"customer_service_answer", "customer_service_inbox"}),
    )

    default_theme = service_router.service_brand_theme(principal=principal)
    assert default_theme.primary_color == "#16191e"

    updated = service_router.update_service_brand_theme(
        service_router.ServiceBrandThemeUpdate(primary_color="#123456", name="Communication"),
        principal=principal,
    )

    assert updated.app_id == "communication"
    assert updated.primary_color == "#123456"
    assert platform.resolve_brand_theme("nft_gym", "communication").primary_color == "#123456"
    assert platform.list_audit("nft_gym")[-1].action == "brand_theme.updated"

    with pytest.raises(HTTPException) as exc:
        service_router.update_service_brand_theme(
            service_router.ServiceBrandThemeUpdate(primary_color="#654321"),
            principal=replace(principal, scopes=frozenset({SCOPE_READ})),
        )
    assert exc.value.status_code == 403


def test_service_intake_routes_multi_space_communication_key_to_customer_service(monkeypatch):
    platform = _platform_store()
    intake_store = MemoryIntakeStore()
    settings = type("Settings", (), {"pii_phase": "dpia_signed", "require_approval": False})()
    pipeline = IntakePipeline(intake_store, settings)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(service_router, "get_intake_pipeline", lambda: pipeline)
    principal = replace(
        _svc_principal(scopes=(SCOPE_WRITE,), tenant="nft_gym"),
        account_id="nft_gym",
        app_id="communication",
        space_ids=frozenset({"sp_customer", "sp_personal"}),
        purposes=frozenset({"customer_service_inbox"}),
    )

    response = service_router.intake(
        ServiceIntakeRequest(
            content="Customer wants to reschedule an appointment.",
            source="communication",
            source_ref="wamid.42",
        ),
        principal=principal,
    )

    assert response.record.space_id == "sp_customer"
    assert response.record.record_type == "message"
    assert response.record.intent == "booking"
    assert response.record.status == "approved"
    assert intake_store.get(response.record.id).source_ref == "wamid.42"
    assert platform.list_audit("nft_gym")[-1].action == "service.intake_access_checked"


def _comm_write_principal():
    return replace(
        _svc_principal(scopes=(SCOPE_WRITE,), tenant="nft_gym"),
        account_id="nft_gym",
        app_id="communication",
        space_ids=frozenset({"sp_customer", "sp_personal"}),
        purposes=frozenset({"customer_service_inbox"}),
    )


def _intake_fixtures(monkeypatch):
    platform = _platform_store()
    intake_store = MemoryIntakeStore()
    settings = type("Settings", (), {"pii_phase": "dpia_signed", "require_approval": False})()
    pipeline = IntakePipeline(intake_store, settings)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(service_router, "get_intake_pipeline", lambda: pipeline)
    monkeypatch.setattr(service_router, "get_intake_store", lambda: intake_store)
    return platform, intake_store


def test_service_records_delete_by_source_ref_is_idempotent(monkeypatch):
    platform, intake_store = _intake_fixtures(monkeypatch)
    principal = _comm_write_principal()

    service_router.intake(
        ServiceIntakeRequest(content="Reschedule please.", source="communication", source_ref="wamid.99"),
        principal=principal,
    )
    assert intake_store.count() == 1

    result = service_router.delete_records(
        service_router.ServiceRecordDeleteRequest(
            source_ref="wamid.99", account_id="nft_gym", space_id="sp_customer",
        ),
        principal=principal,
    )
    assert result.deleted == 1
    assert intake_store.count() == 0
    assert platform.list_audit("nft_gym")[-1].action == "service.records.deleted"

    # Idempotent: an already-gone (or unknown) ref deletes nothing without error.
    again = service_router.delete_records(
        service_router.ServiceRecordDeleteRequest(source_ref="wamid.99", account_id="nft_gym"),
        principal=principal,
    )
    assert again.deleted == 0


def test_service_records_delete_blocked_by_legal_hold(monkeypatch):
    platform, intake_store = _intake_fixtures(monkeypatch)
    principal = _comm_write_principal()
    service_router.intake(
        ServiceIntakeRequest(content="Hold me.", source="communication", source_ref="wamid.hold"),
        principal=principal,
    )
    platform.create_legal_hold(LegalHold(id="h1", account_id="nft_gym", reason="litigation"))

    with pytest.raises(HTTPException) as exc:
        service_router.delete_records(
            service_router.ServiceRecordDeleteRequest(source_ref="wamid.hold", account_id="nft_gym"),
            principal=principal,
        )
    assert exc.value.status_code == 409
    assert intake_store.count() == 1  # preserved under the hold
    assert platform.list_audit("nft_gym")[-1].action == "service.records.delete_denied"


def test_service_records_delete_requires_write_scope_and_own_account(monkeypatch):
    _intake_fixtures(monkeypatch)

    # A read-only key cannot delete.
    with pytest.raises(HTTPException) as read_exc:
        service_router.delete_records(
            service_router.ServiceRecordDeleteRequest(source_ref="x", account_id="nft_gym"),
            principal=_svc_principal(scopes=(SCOPE_READ,), tenant="nft_gym"),
        )
    assert read_exc.value.status_code == 403

    # A write key cannot reach another account.
    with pytest.raises(HTTPException) as acct_exc:
        service_router.delete_records(
            service_router.ServiceRecordDeleteRequest(source_ref="x", account_id="other_account"),
            principal=_comm_write_principal(),
        )
    assert acct_exc.value.status_code == 403


def test_tombstone_feed_is_account_scoped_and_ack_is_idempotent(monkeypatch):
    platform = _platform_store()
    # A second account whose tombstone must NOT appear in nft_gym's feed.
    platform.create_account(Account(id="other", kind="organization", name="Other"))
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)

    mine = platform.create_tombstone(
        Tombstone(id="t1", account_id="nft_gym", target_type="account", reason="offboarding"),
    )
    platform.create_tombstone(Tombstone(id="t_other", account_id="other", target_type="account"))
    principal = _comm_write_principal()

    feed = service_router.list_tombstones(since=0, principal=principal)
    assert [t.id for t in feed.tombstones] == ["t1"]  # the other account's is not visible
    assert feed.cursor == mine.seq

    ack = service_router.ack_tombstone("t1", principal=principal)
    assert ack.tombstone_id == "t1" and ack.app_id == "communication"
    # Idempotent — a second ack is fine and does not duplicate.
    service_router.ack_tombstone("t1", principal=principal)
    assert [a.app_id for a in platform.list_tombstone_acks("t1")] == ["communication"]

    # Polling forward from the cursor returns nothing new.
    assert service_router.list_tombstones(since=feed.cursor, principal=principal).tombstones == []

    # Acking another account's tombstone (or an unknown one) is a 404.
    with pytest.raises(HTTPException) as exc:
        service_router.ack_tombstone("t_other", principal=principal)
    assert exc.value.status_code == 404

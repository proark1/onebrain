"""Service-key auth: the ceiling that lets a non-human caller talk to the brain
without ever reading internal data. These are boundary proofs, like the tenant
tests — a service key must be PUBLIC-ceiled, tenant-pinned, and fail closed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.deps
import app.routers.service as service_router
from app.auth.principal import Principal
from app.auth.principal import resolve_service_principal
from app.auth.roles import ROLES
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.llm.local import LocalLLM
from app.platform.base import Account
from app.platform.memory import MemoryPlatformStore
from app.retrieval.service import RetrievalService
from app.schemas import ServiceKeyCreate
from app.security.policy import CAPTURED_CATEGORY, Classification
from app.servicekeys.base import (
    SCOPE_READ, SCOPE_WRITE, ServiceKey, generate_key, hash_secret, parse_key, verify_secret,
)
from app.servicekeys.memory import MemoryServiceKeyStore
from app.store.memory import MemoryStore


def _admin(tenant: str = "nft_gym") -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id="admin@onebrain",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
        tenant_id=tenant,
    )


def test_key_helpers_roundtrip():
    key_id, secret, plaintext = generate_key()
    assert plaintext == f"sk_{key_id}_{secret}"
    assert parse_key(plaintext) == (key_id, secret)
    h = hash_secret(secret)
    assert verify_secret(secret, h)
    assert not verify_secret("wrong", h)
    assert parse_key("garbage") is None and parse_key("sk_missing") is None and parse_key("") is None


def _store_with_key(monkeypatch, scopes=(SCOPE_READ,), tenant="nft_gym"):
    store = MemoryServiceKeyStore()
    monkeypatch.setattr(app.deps, "get_service_key_store", lambda: store)
    key_id, secret, plaintext = generate_key()
    store.create(ServiceKey(id=key_id, key_hash=hash_secret(secret), tenant_id=tenant, scopes=tuple(scopes)))
    return store, key_id, plaintext


def test_service_principal_is_public_ceiled_and_tenant_pinned(monkeypatch):
    _, _, plaintext = _store_with_key(monkeypatch, scopes=(SCOPE_READ,), tenant="nft_gym")
    p = resolve_service_principal(authorization=f"Bearer {plaintext}")
    assert p.principal_type == "service"
    assert p.clearance == Classification.PUBLIC
    assert p.tenant_id == "nft_gym"
    assert p.categories == frozenset({"general"})
    assert p.has_scope(SCOPE_READ)
    assert p.is_employee is False                       # a service key is never staff

    f = p.access_filter()
    ok = {"tenant_id": "nft_gym", "classification": int(Classification.PUBLIC), "status": "approved"}
    assert f.allows(ok) is True
    assert f.allows({**ok, "classification": int(Classification.INTERNAL)}) is False   # no internal
    assert f.allows({**ok, "category": CAPTURED_CATEGORY, "classification": int(Classification.INTERNAL)}) is False
    assert f.allows({**ok, "tenant_id": "companyB"}) is False                          # no cross-tenant


def test_store_rejects_duplicate_id():
    store = MemoryServiceKeyStore()
    key_id, secret, _ = generate_key()
    store.create(ServiceKey(id=key_id, key_hash=hash_secret(secret), tenant_id="nft_gym", scopes=(SCOPE_READ,)))
    with pytest.raises(ValueError):                     # never silently overwrite a key
        store.create(ServiceKey(id=key_id, key_hash="x", tenant_id="nft_gym", scopes=(SCOPE_READ,)))


def test_memory_service_key_store_summary_counts_statuses_and_tenants():
    store = MemoryServiceKeyStore()
    store.create(ServiceKey(id="key_active", key_hash="x", tenant_id="nft_gym", scopes=(SCOPE_READ,)))
    store.create(ServiceKey(id="key_revoked", key_hash="x", tenant_id="nft_gym", scopes=(SCOPE_WRITE,)))
    store.create(ServiceKey(id="key_other", key_hash="x", tenant_id="other", scopes=(SCOPE_READ,)))
    store.revoke("key_revoked")

    all_keys = store.summary()
    tenant_keys = store.summary("nft_gym")

    assert all_keys.total == 3
    assert all_keys.active == 2
    assert all_keys.revoked == 1
    assert tenant_keys.total == 2
    assert tenant_keys.active == 1
    assert tenant_keys.revoked == 1


def test_memory_service_key_store_records_usage_and_rotates_immediately():
    store = MemoryServiceKeyStore()
    old = store.create(ServiceKey(
        id="key_old",
        key_hash=hash_secret("old"),
        tenant_id="nft_gym",
        scopes=(SCOPE_READ,),
        label="Communication",
        account_id="nft_gym",
        app_id="communication",
        space_ids=("sp_customer",),
        purposes=("customer_service_answer",),
    ))

    used = store.record_usage(old.id, "service.ask")

    assert used.use_count == 1
    assert used.last_used_at
    assert used.last_used_endpoint == "service.ask"

    rotated = store.rotate(
        old.id,
        ServiceKey(
            id="key_new",
            key_hash=hash_secret("new"),
            tenant_id="nft_gym",
            scopes=old.scopes,
            label=old.label,
        ),
    )

    assert rotated.status == "active"
    assert rotated.rotated_from_id == "key_old"
    assert rotated.use_count == 0
    assert rotated.account_id == "nft_gym"
    assert rotated.space_ids == ("sp_customer",)
    assert store.get("key_old").status == "revoked"
    assert store.get("key_old").revoked_at
    with pytest.raises(ValueError, match="inactive"):
        store.rotate("key_old", ServiceKey(id="key_newer", key_hash="x", tenant_id="nft_gym", scopes=(SCOPE_READ,)))


def test_resolve_service_principal_records_usage_after_valid_secret_only(monkeypatch):
    store, key_id, plaintext = _store_with_key(monkeypatch, scopes=(SCOPE_READ,), tenant="nft_gym")

    with pytest.raises(HTTPException):
        resolve_service_principal(authorization=f"Bearer sk_{key_id}_wrong")
    assert store.get(key_id).use_count == 0
    assert store.get(key_id).last_used_at == ""

    principal = resolve_service_principal(authorization=f"Bearer {plaintext}")

    assert principal.user_id == f"svc:{key_id}"
    assert store.get(key_id).use_count == 1
    assert store.get(key_id).last_used_at
    assert store.get(key_id).last_used_endpoint == "service.auth"


def test_missing_invalid_and_revoked_keys_fail_closed(monkeypatch):
    store, key_id, plaintext = _store_with_key(monkeypatch)
    for bad in ("", "Bearer ", "Bearer not-a-key", f"Bearer sk_{key_id}_wrongsecret"):
        with pytest.raises(HTTPException) as e:
            resolve_service_principal(authorization=bad)
        assert e.value.status_code == 401
    # a valid key works, then revocation denies it
    assert resolve_service_principal(authorization=f"Bearer {plaintext}").tenant_id == "nft_gym"
    store.revoke(key_id)
    with pytest.raises(HTTPException) as e:
        resolve_service_principal(authorization=f"Bearer {plaintext}")
    assert e.value.status_code == 401


def test_service_key_management_audits_mint_revoke_and_rotate(monkeypatch):
    keys = MemoryServiceKeyStore()
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="nft_gym", kind="organization", name="NFT Gym"))
    monkeypatch.setattr(app.deps, "get_service_key_store", lambda: keys)
    monkeypatch.setattr(service_router, "get_service_key_store", lambda: keys)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)

    minted = service_router.mint_key(
        ServiceKeyCreate(
            scopes=[SCOPE_READ],
            label="Communication integration",
            app_id="communication",
            space_ids=["sp_customer"],
            purposes=["customer_service_answer"],
        ),
        principal=_admin(),
    )

    assert minted.key.startswith(f"sk_{minted.id}_")
    assert minted.rotated_from_id == ""
    assert keys.get(minted.id).status == "active"
    listed = service_router.list_keys(principal=_admin())
    assert listed[0].id == minted.id
    assert listed[0].use_count == 0
    assert not hasattr(listed[0], "key")
    assert not hasattr(listed[0], "key_hash")

    rotated = service_router.rotate_key(minted.id, principal=_admin())

    assert rotated.id != minted.id
    assert rotated.key.startswith(f"sk_{rotated.id}_")
    assert rotated.rotated_from_id == minted.id
    assert keys.get(minted.id).status == "revoked"
    assert keys.get(minted.id).revoked_at
    assert keys.get(rotated.id).status == "active"
    assert keys.get(rotated.id).rotated_from_id == minted.id
    with pytest.raises(HTTPException) as exc:
        resolve_service_principal(authorization=f"Bearer {minted.key}")
    assert exc.value.status_code == 401
    assert resolve_service_principal(authorization=f"Bearer {rotated.key}").user_id == f"svc:{rotated.id}"

    service_router.revoke_key(rotated.id, principal=_admin())

    actions = [event.action for event in platform.list_audit("nft_gym")]
    assert actions == ["service_key.minted", "service_key.rotated", "service_key.revoked"]
    audit_dump = str([event.meta for event in platform.list_audit("nft_gym")])
    assert minted.key not in audit_dump
    assert rotated.key not in audit_dump
    assert "sha256$" not in audit_dump


def test_service_key_rotate_is_admin_and_tenant_scoped(monkeypatch):
    keys = MemoryServiceKeyStore()
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="nft_gym", kind="organization", name="NFT Gym"))
    key = keys.create(ServiceKey(id="key_a", key_hash="x", tenant_id="nft_gym", scopes=(SCOPE_READ,)))
    monkeypatch.setattr(service_router, "get_service_key_store", lambda: keys)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)

    with pytest.raises(HTTPException) as exc:
        service_router.rotate_key(key.id, principal=_svc_principal())
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        service_router.rotate_key(key.id, principal=_admin("other"))
    assert exc.value.status_code == 404

    keys.revoke(key.id)
    with pytest.raises(HTTPException) as exc:
        service_router.rotate_key(key.id, principal=_admin())
    assert exc.value.status_code == 409


def _svc_principal(tenant="nft_gym", scopes=(SCOPE_READ,)):
    from app.auth.principal import Principal
    return Principal(
        user_id="svc:1", role_id="service", role_label="Service", clearance=Classification.PUBLIC,
        locations=frozenset(), categories=frozenset({"general"}), location_label="—",
        tenant_id=tenant, principal_type="service", scopes=frozenset(scopes),
    )


def test_sources_are_stripped_for_service_principals():
    emb, store = LocalEmbedder(), MemoryStore()
    IngestPipeline(emb, store).ingest_text(
        title="Hours", text="The gym opens at 06:00 and closes at 23:00.", classification="public",
        location="global", category="general", uploaded_by="seed", tenant="nft_gym",
    )
    svc = RetrievalService(emb, store, LocalLLM())

    service_events = list(svc.answer_stream(_svc_principal(), "when does the gym open?"))
    service_sources = next(e for e in service_events if e["type"] == "sources")["sources"]
    assert service_sources == []                        # stripped for a service caller

    from app.auth.principal import Principal
    human = Principal(user_id="u1", role_id="admin", role_label="Admin", clearance=Classification.RESTRICTED,
                      locations=None, categories=None, location_label="all", tenant_id="nft_gym")
    human_events = list(svc.answer_stream(human, "when does the gym open?"))
    human_sources = next(e for e in human_events if e["type"] == "sources")["sources"]
    assert len(human_sources) >= 1                       # a human still gets sources


def test_captured_content_is_unreadable_by_a_read_key():
    # A write:capture push lands INTERNAL/captured_input; a read:public key (or any
    # ordinary staff role) can never retrieve it.
    emb, store = LocalEmbedder(), MemoryStore()
    IngestPipeline(emb, store).ingest_text(
        title="captured", text="A customer asked about refund policy in a private message.",
        classification="internal", location="global", category=CAPTURED_CATEGORY,
        uploaded_by="svc:1", tenant="nft_gym",
    )
    read_key = _svc_principal(scopes=(SCOPE_READ,))
    hits = store.search(emb.embed_one("refund policy"), 5, read_key.access_filter())
    assert hits == []                                   # captured content is invisible to the read key

"""Service-key auth: the ceiling that lets a non-human caller talk to the brain
without ever reading internal data. These are boundary proofs, like the tenant
tests — a service key must be PUBLIC-ceiled, tenant-pinned, and fail closed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.deps
from app.auth.principal import resolve_service_principal
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.llm.local import LocalLLM
from app.retrieval.service import RetrievalService
from app.security.policy import CAPTURED_CATEGORY, Classification
from app.servicekeys.base import (
    SCOPE_READ, SCOPE_WRITE, ServiceKey, generate_key, hash_secret, parse_key, verify_secret,
)
from app.servicekeys.memory import MemoryServiceKeyStore
from app.store.memory import MemoryStore


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

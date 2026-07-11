"""Personal/family spaces are owner-only, end to end.

Closes the leak where a human's unscoped retrieval (account_id="", space_ids=None)
saw every space in the tenant — including colleagues' personal spaces. The fix
stamps space_kind + owner_user_id at ingest and enforces owner-only reads in the
access filter, so a personal-space chunk never reaches a non-owner, an admin, or a
service key through retrieval.
"""

from __future__ import annotations

import app.deps as deps
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.deps import _resolve_space_kind_and_owner
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.llm.local import LocalLLM
from app.platform.base import Account, Membership, Space
from app.platform.memory import MemoryPlatformStore
from app.retrieval.service import RetrievalService
from app.store.memory import MemoryStore


def _human(user_id: str, role_id: str = "admin", tenant: str = "nft_gym") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=user_id,
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"munich"}),
        categories=role.categories,
        location_label="munich",
        tenant_id=tenant,
    )


def _platform_with_personal_space(owner_user_id: str = "alice", *, member: str = "") -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="acct", kind="person", name="Alice Co", owner_user_id=owner_user_id))
    platform.create_space(Space(id="sp_personal", account_id="acct", kind="personal", name="Alice private"))
    platform.create_space(Space(id="sp_shared", account_id="acct", kind="business", name="Company shared"))
    if member:
        platform.upsert_membership(Membership(id="m1", account_id="acct", user_id=member,
                                              role_id="employee", space_id="sp_personal"))
    return platform


# --- the deps resolver: space_id -> (kind, owner) --------------------------------

def test_resolver_uses_account_owner_for_personal_space(monkeypatch):
    monkeypatch.setattr(deps, "get_platform_store", lambda: _platform_with_personal_space("alice"))
    assert _resolve_space_kind_and_owner("sp_personal") == ("personal", "alice")


def test_resolver_prefers_space_membership_over_account_owner(monkeypatch):
    # A per-employee personal space is owned by the member bound to it, not the
    # account owner — so an account with many employees resolves each personal
    # space to the right person.
    monkeypatch.setattr(deps, "get_platform_store",
                        lambda: _platform_with_personal_space("account_owner", member="bob"))
    assert _resolve_space_kind_and_owner("sp_personal") == ("personal", "bob")


def test_resolver_returns_no_owner_for_non_private_space(monkeypatch):
    monkeypatch.setattr(deps, "get_platform_store", lambda: _platform_with_personal_space("alice"))
    assert _resolve_space_kind_and_owner("sp_shared") == ("business", "")


def test_resolver_is_safe_on_unknown_space(monkeypatch):
    monkeypatch.setattr(deps, "get_platform_store", lambda: _platform_with_personal_space("alice"))
    assert _resolve_space_kind_and_owner("nope") == ("", "")


# --- end-to-end retrieval --------------------------------------------------------

def _ingest_alice_personal_note(monkeypatch) -> MemoryStore:
    monkeypatch.setattr(deps, "get_platform_store", lambda: _platform_with_personal_space("alice"))
    store = MemoryStore()
    pipe = IngestPipeline(LocalEmbedder(), store, space_resolver=_resolve_space_kind_and_owner)
    pipe.ingest_text(
        title="Alice personal note",
        text="Alice's private salary expectation for the next review is ninety thousand euros.",
        classification="public",           # gate is ownership, not clearance
        location="global",
        category="general",
        uploaded_by="alice",
        tenant="nft_gym",
        account_id="acct",
        space_id="sp_personal",
    )
    return store


def _titles(hits) -> set[str]:
    return {h.chunk.meta.get("doc_title") for h in hits}


def test_owner_can_retrieve_own_personal_space(monkeypatch):
    store = _ingest_alice_personal_note(monkeypatch)
    service = RetrievalService(LocalEmbedder(), store, LocalLLM(), top_k=8, min_score=0.0)
    hits = service.retrieve(_human("alice"), "salary expectation")
    assert "Alice personal note" in _titles(hits)


def test_colleague_and_admin_cannot_retrieve_personal_space(monkeypatch):
    store = _ingest_alice_personal_note(monkeypatch)
    service = RetrievalService(LocalEmbedder(), store, LocalLLM(), top_k=8, min_score=0.0)
    # A different employee AND a maximally-cleared admin both come up empty — this
    # is the leak that used to succeed via unscoped (space_ids=None) retrieval.
    assert "Alice personal note" not in _titles(service.retrieve(_human("bob"), "salary expectation"))
    assert "Alice personal note" not in _titles(service.retrieve(_human("admin@nft_gym"), "salary expectation"))


def test_personal_chunk_is_stamped_with_kind_and_owner(monkeypatch):
    store = _ingest_alice_personal_note(monkeypatch)
    metas = [c.meta for c in store._chunks]  # noqa: SLF001 — white-box check of ingest labelling
    assert metas and all(m.get("space_kind") == "personal" for m in metas)
    assert all(m.get("owner_user_id") == "alice" for m in metas)

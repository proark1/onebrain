"""Publication lifecycle at the pipeline + store level: quarantine on upload,
auto-quarantine of PUBLIC-with-PII, and approval making content reachable.
The HTTP four-eyes rule is exercised separately (router-level).
"""

from __future__ import annotations

from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.security.policy import AccessFilter, Classification
from app.store.memory import MemoryStore

_ADMIN = AccessFilter("nft_gym", int(Classification.RESTRICTED), None, None)


def _pipe():
    store = MemoryStore()
    return IngestPipeline(LocalEmbedder(), store), store


def _titles(store):
    return {d["title"] for d in store.list_documents(_ADMIN)}


def test_require_approval_quarantines_until_approved():
    pipe, store = _pipe()
    r = pipe.ingest_text(
        title="Cleaning SOP", text="Wipe the mats after each class.", classification="internal",
        location="global", category="ops", uploaded_by="u1", tenant="nft_gym", require_approval=True,
    )
    assert r.status == "pending"
    assert "Cleaning SOP" not in _titles(store)              # not reachable while pending
    assert store.list_pending("nft_gym")[0]["doc_id"] == r.doc_id

    store.set_document_status(r.doc_id, "approved", approved_by="u2")
    assert "Cleaning SOP" in _titles(store)                  # now live
    assert store.list_pending("nft_gym") == []


def test_public_upload_with_pii_is_auto_quarantined():
    pipe, store = _pipe()
    r = pipe.ingest_text(
        title="Newsletter", text="Questions? Email erika@example.de.", classification="public",
        location="global", category="general", uploaded_by="u1", tenant="nft_gym",
    )
    assert r.status == "pending" and r.pii_findings        # PII into PUBLIC never auto-live
    assert "Newsletter" not in _titles(store)


def test_clean_upload_goes_live_immediately():
    pipe, store = _pipe()
    r = pipe.ingest_text(
        title="Hours", text="Open 06:00 to 23:00. 49 EUR per month.", classification="public",
        location="global", category="general", uploaded_by="u1", tenant="nft_gym",
    )
    assert r.status == "approved"
    assert "Hours" in _titles(store)


def test_pending_is_tenant_scoped():
    pipe, store = _pipe()
    pipe.ingest_text(title="A", text="x", classification="internal", location="global",
                     category="general", uploaded_by="u1", tenant="nft_gym", require_approval=True)
    pipe.ingest_text(title="B", text="y", classification="internal", location="global",
                     category="general", uploaded_by="u1", tenant="companyB", require_approval=True)
    assert {d["title"] for d in store.list_pending("nft_gym")} == {"A"}
    assert {d["title"] for d in store.list_pending("companyB")} == {"B"}

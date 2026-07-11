"""In-process vector store with optional pickle persistence.

The access filter is applied BEFORE scoring, so unauthorised chunks are never
even ranked — the same guarantee a database WHERE clause gives you. Good for a
prototype and for tests; swap in `PgVectorStore` for production.
"""

from __future__ import annotations

import os
import pickle
import threading
from typing import List, Optional

import numpy as np

from app.security.policy import AccessFilter
from app.store.base import Chunk, Hit


def _matches_privacy_scope(meta: dict, tenant_id: str, account_id: str = "", space_id: str = "") -> bool:
    if meta.get("tenant_id") != tenant_id:
        return False
    meta_account = meta.get("account_id", "")
    if space_id:
        return meta_account == account_id and meta.get("space_id", "") == space_id
    if account_id and meta_account not in ("", account_id):
        return False
    return True


class MemoryStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._chunks: List[Chunk] = []
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self) -> None:
        if self._persist_path and os.path.exists(self._persist_path):
            with open(self._persist_path, "rb") as fh:
                self._chunks = pickle.load(fh)

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "wb") as fh:
            pickle.dump(self._chunks, fh)

    # --- api -------------------------------------------------------------
    def add(self, chunks: List[Chunk]) -> None:
        with self._lock:
            self._chunks.extend(chunks)
            self._save()

    def search(self, query: np.ndarray, k: int, access: AccessFilter) -> List[Hit]:
        with self._lock:
            allowed = [c for c in self._chunks if c.embedding is not None and access.allows(c.meta)]
        if not allowed:
            return []
        matrix = np.vstack([c.embedding for c in allowed])
        scores = matrix @ query  # normalised vectors => cosine similarity
        order = np.argsort(-scores)[:k]
        return [Hit(chunk=allowed[i], score=float(scores[i])) for i in order]

    def list_documents(self, access: AccessFilter) -> List[dict]:
        docs: dict[str, dict] = {}
        with self._lock:
            for c in self._chunks:
                if not access.allows(c.meta):
                    continue
                doc = docs.setdefault(c.doc_id, {
                    "doc_id": c.doc_id,
                    "title": c.meta.get("doc_title", "Untitled"),
                    "classification": c.meta.get("classification_label", "internal"),
                    "location": c.meta.get("location", "global"),
                    "category": c.meta.get("category", "general"),
                    "account_id": c.meta.get("account_id", ""),
                    "space_id": c.meta.get("space_id", ""),
                    "chunks": 0,
                })
                doc["chunks"] += 1
        return sorted(docs.values(), key=lambda d: d["title"].lower())

    def list_pending(self, tenant_id: str) -> List[dict]:
        docs: dict[str, dict] = {}
        with self._lock:
            for c in self._chunks:
                if c.meta.get("tenant_id") != tenant_id:
                    continue
                if c.meta.get("status", "approved") == "approved":
                    continue
                doc = docs.setdefault(c.doc_id, {
                    "doc_id": c.doc_id,
                    "title": c.meta.get("doc_title", "Untitled"),
                    "classification": int(c.meta.get("classification", 3)),
                    "classification_label": c.meta.get("classification_label", "internal"),
                    "location": c.meta.get("location", "global"),
                    "category": c.meta.get("category", "general"),
                    "account_id": c.meta.get("account_id", ""),
                    "space_id": c.meta.get("space_id", ""),
                    "uploaded_by": c.meta.get("uploaded_by", ""),
                    "status": c.meta.get("status", "pending"),
                    "has_pii": False,
                    "chunks": 0,
                })
                doc["chunks"] += 1
                if c.meta.get("pii_findings"):
                    doc["has_pii"] = True
        return sorted(docs.values(), key=lambda d: d["title"].lower())

    def get_document_meta(self, doc_id: str) -> Optional[dict]:
        with self._lock:
            chunks = [c for c in self._chunks if c.doc_id == doc_id]
        if not chunks:
            return None
        first = chunks[0]
        return {
            "doc_id": doc_id,
            "title": first.meta.get("doc_title", "Untitled"),
            "tenant_id": first.meta.get("tenant_id", ""),
            "classification": int(first.meta.get("classification", 3)),
            "classification_label": first.meta.get("classification_label", "internal"),
            "location": first.meta.get("location", "global"),
            "category": first.meta.get("category", "general"),
            "account_id": first.meta.get("account_id", ""),
            "space_id": first.meta.get("space_id", ""),
            "uploaded_by": first.meta.get("uploaded_by", ""),
            "status": first.meta.get("status", "approved"),
            "chunks": len(chunks),
        }

    def set_document_status(self, doc_id: str, status: str, approved_by: Optional[str] = None) -> int:
        with self._lock:
            changed = 0
            for c in self._chunks:
                if c.doc_id == doc_id:
                    c.meta["status"] = status
                    if approved_by is not None:
                        c.meta["approved_by"] = approved_by
                    changed += 1
            if changed:
                self._save()
        return changed

    def delete_document(self, doc_id: str) -> int:
        with self._lock:
            before = len(self._chunks)
            self._chunks = [c for c in self._chunks if c.doc_id != doc_id]
            removed = before - len(self._chunks)
            self._save()
        return removed

    def export_documents(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[dict]:
        docs: dict[str, dict] = {}
        with self._lock:
            chunks = [c for c in self._chunks if _matches_privacy_scope(c.meta, tenant_id, account_id, space_id)]
        for c in chunks:
            doc = docs.setdefault(c.doc_id, {
                "doc_id": c.doc_id,
                "title": c.meta.get("doc_title", "Untitled"),
                "tenant_id": c.meta.get("tenant_id", ""),
                "account_id": c.meta.get("account_id", ""),
                "space_id": c.meta.get("space_id", ""),
                "classification": c.meta.get("classification_label", "internal"),
                "location": c.meta.get("location", "global"),
                "category": c.meta.get("category", "general"),
                "status": c.meta.get("status", "approved"),
                "uploaded_by": c.meta.get("uploaded_by", ""),
                "chunks": [],
            })
            doc["chunks"].append({
                "id": c.id,
                "text": c.text,
                "meta": dict(c.meta),
            })
        return sorted(docs.values(), key=lambda d: d["title"].lower())

    def delete_documents_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "",
                                  older_than: str = "") -> dict:
        # older_than="" (the erase path) removes the whole scope. When set (the
        # retention path) only chunks stamped with a created_at strictly before the
        # cutoff go — a chunk with no created_at is never aged out.
        def _match(chunk) -> bool:
            if not _matches_privacy_scope(chunk.meta, tenant_id, account_id, space_id):
                return False
            if older_than:
                ts = chunk.meta.get("created_at", "")
                return bool(ts) and ts < older_than
            return True

        with self._lock:
            keep, removed_chunks = [], []
            for chunk in self._chunks:
                (removed_chunks if _match(chunk) else keep).append(chunk)
            removed_doc_ids = {c.doc_id for c in removed_chunks}
            if removed_chunks:
                self._chunks = keep
                self._save()
        return {"documents": len(removed_doc_ids), "chunks": len(removed_chunks)}

    def count(self) -> int:
        with self._lock:
            return len(self._chunks)

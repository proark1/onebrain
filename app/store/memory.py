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
                    "chunks": 0,
                })
                doc["chunks"] += 1
        return sorted(docs.values(), key=lambda d: d["title"].lower())

    def delete_document(self, doc_id: str) -> int:
        with self._lock:
            before = len(self._chunks)
            self._chunks = [c for c in self._chunks if c.doc_id != doc_id]
            removed = before - len(self._chunks)
            self._save()
        return removed

    def count(self) -> int:
        with self._lock:
            return len(self._chunks)

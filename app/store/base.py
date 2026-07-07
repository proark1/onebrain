"""Vector store interface and shared data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol

import numpy as np

from app.security.policy import AccessFilter


@dataclass
class Chunk:
    id: str
    doc_id: str
    text: str
    meta: dict
    embedding: Optional[np.ndarray] = None


@dataclass
class Hit:
    chunk: Chunk
    score: float


class VectorStore(Protocol):
    def add(self, chunks: List[Chunk]) -> None: ...

    def search(self, query: np.ndarray, k: int, access: AccessFilter) -> List[Hit]: ...

    def list_documents(self, access: AccessFilter) -> List[dict]: ...

    def list_pending(self, tenant_id: str) -> List[dict]: ...

    def set_document_status(self, doc_id: str, status: str, approved_by: Optional[str] = None) -> int: ...

    def delete_document(self, doc_id: str) -> int: ...

    def count(self) -> int: ...

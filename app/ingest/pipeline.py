"""Ingestion pipeline: extract -> chunk -> label -> embed -> store.

The label (classification, location, category) is stamped onto every chunk here
at ingest time — that metadata is exactly what the access filter reads later, so
labelling is what makes gating possible. The document title is folded into the
embedded text (but not the stored text) to sharpen retrieval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.ingest.chunk import chunk_text
from app.ingest.extract import extract_text
from app.security.pii import scan_pii
from app.security.policy import GENERAL_CATEGORY, GLOBAL_LOCATION, STATUS_APPROVED, STATUS_PENDING, Classification
from app.store.base import Chunk


@dataclass
class IngestResult:
    doc_id: str
    title: str
    classification: str
    location: str
    category: str
    chunks: int
    status: str = STATUS_APPROVED
    pii_findings: list = field(default_factory=list)


class IngestPipeline:
    def __init__(self, embedder, store):
        self._embedder = embedder
        self._store = store

    def ingest_text(self, *, title, text, classification, location, category, uploaded_by, tenant,
                    require_approval=False, block_public_on_pii=True) -> IngestResult:
        cls = Classification.parse(classification)
        location = (location or GLOBAL_LOCATION).strip().lower() or GLOBAL_LOCATION
        category = (category or GENERAL_CATEGORY).strip().lower() or GENERAL_CATEGORY
        # Tenant is an exact canonical id from a controlled source (a pinned
        # constant or a service-key record) — NOT lowercased, so it matches the
        # principal's tenant_id verbatim on read. Only whitespace is trimmed.
        tenant = (tenant or "").strip()
        if not tenant:
            raise ValueError("tenant is required — a chunk with no tenant is unreachable.")

        pieces = chunk_text(text)
        if not pieces:
            raise ValueError("No extractable text in document.")

        # Publication lifecycle: decide whether this upload goes live immediately
        # or lands in quarantine for a second pair of eyes. A mislabel, or a PII
        # leak into PUBLIC, is caught HERE — before the content is reachable.
        pii_findings = scan_pii(text)
        if require_approval:
            status = STATUS_PENDING
        elif pii_findings and cls == Classification.PUBLIC and block_public_on_pii:
            status = STATUS_PENDING          # a PUBLIC upload carrying PII is never auto-live
        else:
            status = STATUS_APPROVED

        vectors = self._embedder.embed([f"{title}. {piece}" for piece in pieces])
        doc_id = uuid.uuid4().hex
        chunks = [
            Chunk(
                id=f"{doc_id}:{i}",
                doc_id=doc_id,
                text=piece,
                meta={
                    "tenant_id": tenant,
                    "doc_title": title,
                    "classification": int(cls),
                    "classification_label": cls.name.lower(),
                    "location": location,
                    "category": category,
                    "chunk_index": i,
                    "uploaded_by": uploaded_by,
                    "status": status,
                    "pii_findings": pii_findings,
                },
                embedding=vector,
            )
            for i, (piece, vector) in enumerate(zip(pieces, vectors))
        ]
        self._store.add(chunks)
        return IngestResult(doc_id, title, cls.name.lower(), location, category, len(chunks),
                            status=status, pii_findings=pii_findings)

    def ingest_file(self, *, filename, data, classification, location, category, uploaded_by, tenant,
                    require_approval=False, block_public_on_pii=True) -> IngestResult:
        text = extract_text(filename, data)
        return self.ingest_text(
            title=filename, text=text, classification=classification,
            location=location, category=category, uploaded_by=uploaded_by, tenant=tenant,
            require_approval=require_approval, block_public_on_pii=block_public_on_pii,
        )

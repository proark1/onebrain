"""Ingestion pipeline: extract -> chunk -> label -> embed -> store.

The label (classification, location, category) is stamped onto every chunk here
at ingest time — that metadata is exactly what the access filter reads later, so
labelling is what makes gating possible. The document title is folded into the
embedded text (but not the stored text) to sharpen retrieval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.ingest.chunk import chunk_text
from app.ingest.extract import extract_text
from app.security.policy import GENERAL_CATEGORY, GLOBAL_LOCATION, Classification
from app.store.base import Chunk


@dataclass
class IngestResult:
    doc_id: str
    title: str
    classification: str
    location: str
    category: str
    chunks: int


class IngestPipeline:
    def __init__(self, embedder, store):
        self._embedder = embedder
        self._store = store

    def ingest_text(self, *, title, text, classification, location, category, uploaded_by) -> IngestResult:
        cls = Classification.parse(classification)
        location = (location or GLOBAL_LOCATION).strip().lower() or GLOBAL_LOCATION
        category = (category or GENERAL_CATEGORY).strip().lower() or GENERAL_CATEGORY

        pieces = chunk_text(text)
        if not pieces:
            raise ValueError("No extractable text in document.")

        vectors = self._embedder.embed([f"{title}. {piece}" for piece in pieces])
        doc_id = uuid.uuid4().hex
        chunks = [
            Chunk(
                id=f"{doc_id}:{i}",
                doc_id=doc_id,
                text=piece,
                meta={
                    "doc_title": title,
                    "classification": int(cls),
                    "classification_label": cls.name.lower(),
                    "location": location,
                    "category": category,
                    "chunk_index": i,
                    "uploaded_by": uploaded_by,
                },
                embedding=vector,
            )
            for i, (piece, vector) in enumerate(zip(pieces, vectors))
        ]
        self._store.add(chunks)
        return IngestResult(doc_id, title, cls.name.lower(), location, category, len(chunks))

    def ingest_file(self, *, filename, data, classification, location, category, uploaded_by) -> IngestResult:
        text = extract_text(filename, data)
        return self.ingest_text(
            title=filename, text=text, classification=classification,
            location=location, category=category, uploaded_by=uploaded_by,
        )

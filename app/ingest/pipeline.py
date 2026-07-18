"""Ingestion pipeline: extract -> chunk -> label -> embed -> store.

The label (classification, location, category) is stamped onto every chunk here
at ingest time — that metadata is exactly what the access filter reads later, so
labelling is what makes gating possible. The document title is folded into the
embedded text (but not the stored text) to sharpen retrieval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.ingest.chunk import chunk_text
from app.ingest.extract import extract_text_isolated
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
    def __init__(self, embedder, store, space_resolver=None):
        self._embedder = embedder
        self._store = store
        # Optional callable: space_id -> (space_kind, owner_user_id). Wired in
        # deps.get_pipeline so every production ingest path (upload, capture,
        # async worker, seed) stamps a space's kind and — for private spaces —
        # its owner, without each caller having to look it up. Left None in unit
        # tests, which pass space_kind/owner_user_id explicitly when they need them.
        self._space_resolver = space_resolver

    def ingest_text(self, *, title, text, classification, location, category, uploaded_by, tenant,
                    require_approval=False, block_public_on_pii=True, pii_phase="dpia_signed",
                    account_id: str = "", space_id: str = "",
                    space_kind: str = "", owner_user_id: str = "",
                    idempotency_key: str = "") -> IngestResult:
        cls = Classification.parse(classification)
        location = (location or GLOBAL_LOCATION).strip().lower() or GLOBAL_LOCATION
        category = (category or GENERAL_CATEGORY).strip().lower() or GENERAL_CATEGORY
        # Tenant is an exact canonical id from a controlled source (a pinned
        # constant or a service-key record) — NOT lowercased, so it matches the
        # principal's tenant_id verbatim on read. Only whitespace is trimmed.
        tenant = (tenant or "").strip()
        if not tenant:
            raise ValueError("tenant is required — a chunk with no tenant is unreachable.")

        idempotency_key = (idempotency_key or "").strip()
        # Keep the recovered result byte-for-byte equivalent to the original
        # job result without re-writing its chunks.
        pii_findings = scan_pii(text)
        doc_id = (
            uuid.uuid5(uuid.NAMESPACE_URL, f"onebrain:ingest:{idempotency_key}").hex
            if idempotency_key
            else uuid.uuid4().hex
        )
        if idempotency_key:
            existing = self._store.get_document_meta(doc_id)
            if existing is not None:
                if (
                    existing.get("tenant_id", "") != tenant
                    or existing.get("account_id", "") != (account_id or "").strip()
                    or existing.get("space_id", "") != (space_id or "").strip()
                ):
                    raise ValueError("ingestion idempotency key is already bound to another scope")
                return IngestResult(
                    doc_id=doc_id,
                    title=existing.get("title", title),
                    classification=existing.get("classification_label", "internal"),
                    location=existing.get("location", GLOBAL_LOCATION),
                    category=existing.get("category", GENERAL_CATEGORY),
                    chunks=int(existing.get("chunks", 0)),
                    status=existing.get("status", STATUS_APPROVED),
                    pii_findings=pii_findings,
                )

        account_id = (account_id or "").strip()
        space_id = (space_id or "").strip()
        space_kind = (space_kind or "").strip()
        owner_user_id = (owner_user_id or "").strip()
        # Resolve the space's kind and owner once, unless the caller supplied them.
        # A private space with no resolvable owner stays owner="" — which the access
        # filter treats as owned-by-nobody (fail closed), never as world-readable.
        if space_id and self._space_resolver is not None and not (space_kind and owner_user_id):
            resolved_kind, resolved_owner = self._space_resolver(space_id)
            space_kind = space_kind or (resolved_kind or "").strip()
            owner_user_id = owner_user_id or (resolved_owner or "").strip()

        pieces = chunk_text(text)
        if not pieces:
            raise ValueError("No extractable text in document.")

        # Publication lifecycle: decide whether this upload goes live immediately
        # or lands in quarantine for a second pair of eyes. A mislabel, or a PII
        # leak into PUBLIC, is caught HERE — before the content is reachable.
        # Synthetic-data phase interlock: before a signed DPIA, real personal data
        # must not enter the system AT ALL — the scanner is the tripwire, so a
        # careless upload of a real member/employee file is refused, not just parked.
        if pii_phase == "synthetic" and pii_findings:
            kinds = ", ".join(sorted({f["type"] for f in pii_findings}))
            raise ValueError(
                f"Personal data detected ({kinds}). This deployment is in synthetic-data mode "
                "(ONEBRAIN_PII_PHASE=synthetic); a signed DPIA is required before real personal "
                "data can be ingested."
            )
        if require_approval:
            status = STATUS_PENDING
        elif pii_findings and cls == Classification.PUBLIC and block_public_on_pii:
            status = STATUS_PENDING          # a PUBLIC upload carrying PII is never auto-live
        else:
            status = STATUS_APPROVED

        vectors = self._embedder.embed([f"{title}. {piece}" for piece in pieces])
        # A single ingest timestamp for the whole document. Retention filters on
        # this: a chunk with no created_at (pre-Phase-1b) is never aged out, so
        # retention can only ever delete records it can prove are old enough.
        created_at = datetime.now(timezone.utc).isoformat()
        chunks = []
        for i, (piece, vector) in enumerate(zip(pieces, vectors)):
            meta = {
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
                "created_at": created_at,
            }
            if idempotency_key:
                meta["job_idempotency_key"] = idempotency_key
            if account_id:
                meta["account_id"] = account_id
            if space_id:
                meta["space_id"] = space_id
            # space_kind + owner_user_id drive the private-space owner rule in
            # AccessFilter. Only stamped when known; a chunk in a private space
            # therefore always carries both (owner may be "" = owned-by-nobody).
            if space_kind:
                meta["space_kind"] = space_kind
            if owner_user_id:
                meta["owner_user_id"] = owner_user_id
            chunks.append(Chunk(
                id=f"{doc_id}:{i}",
                doc_id=doc_id,
                text=piece,
                meta=meta,
                embedding=vector,
            ))
        self._store.add(chunks)
        return IngestResult(doc_id, title, cls.name.lower(), location, category, len(chunks),
                            status=status, pii_findings=pii_findings)

    def ingest_file(self, *, filename, data, classification, location, category, uploaded_by, tenant,
                    require_approval=False, block_public_on_pii=True, pii_phase="dpia_signed",
                    account_id: str = "", space_id: str = "",
                    space_kind: str = "", owner_user_id: str = "",
                    idempotency_key: str = "") -> IngestResult:
        text = extract_text_isolated(filename, data)
        return self.ingest_text(
            title=filename, text=text, classification=classification,
            location=location, category=category, uploaded_by=uploaded_by, tenant=tenant,
            require_approval=require_approval, block_public_on_pii=block_public_on_pii, pii_phase=pii_phase,
            account_id=account_id, space_id=space_id,
            space_kind=space_kind, owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
        )

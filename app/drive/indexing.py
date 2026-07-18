"""Generation-fenced Drive extraction, approval, embedding, and publication."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from app.drive.base import DriveGenerationConflict
from app.ingest.chunk import chunk_text
from app.ingest.extract import UnsupportedDocumentError, extract_text_isolated
from app.security.pii import scan_pii
from app.security.policy import STATUS_APPROVED, Classification
from app.store.base import Chunk


MAX_DRIVE_CHUNKS = 20_000
EMBED_BATCH_SIZE = 64


def handle_drive_index_job(job) -> dict:
    from app.config import get_settings
    from app.deps import get_drive_blob_store, get_drive_store, get_embedder

    payload = job.payload
    file_id = str(payload.get("file_id") or "")
    revision_id = str(payload.get("revision_id") or "")
    try:
        generation = int(payload.get("generation"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Drive indexing job has an invalid generation.") from exc
    if not file_id or not revision_id or generation < 1:
        raise ValueError("Drive indexing job is missing file identity.")

    store = get_drive_store()
    file = store.get_file(file_id, account_id=job.account_id, space_id=job.space_id)
    if not file or file.current_revision_id != revision_id or file.generation != generation:
        return {"status": "stale", "file_id": file_id, "generation": generation}
    if file.trashed_at or not file.desired_indexed:
        return {"status": "not_indexed", "file_id": file_id, "generation": generation}
    revision = store.get_revision(revision_id, account_id=job.account_id, space_id=job.space_id)
    if not revision or revision.file_id != file.id:
        raise ValueError("Drive indexing revision is missing.")

    file = _set_state(store, file, "extracting")
    blob = get_drive_blob_store()
    info = blob.stat(revision.storage_key)
    if not info or info.sha256 != revision.sha256 or info.size_bytes != revision.size_bytes:
        raise ValueError("Drive original is missing or does not match its revision.")
    data = b"".join(blob.iter_range(revision.storage_key))
    try:
        text = extract_text_isolated(file.name, data)
    except UnsupportedDocumentError as exc:
        _set_state(store, file, "unsupported")
        return {"status": "unsupported", "file_id": file.id, "reason": str(exc)}

    # The filename is shown to and embedded for the model, so it belongs to the
    # same PII gate as extracted body text. Otherwise `alice@example.com.pdf`
    # bypasses synthetic-mode quarantine despite entering chunk metadata/input.
    findings = scan_pii(f"{file.name}\n{text}")
    settings = get_settings()
    if settings.pii_phase == "synthetic" and findings:
        _set_state(store, file, "blocked")
        return {
            "status": "blocked",
            "file_id": file.id,
            "reason": "personal_data_in_synthetic_mode",
            "pii_types": sorted({row["type"] for row in findings}),
        }

    classification = Classification.parse(file.classification)
    needs_review = bool(settings.require_approval) or bool(
        findings and classification == Classification.PUBLIC and settings.block_public_on_pii
    )
    if needs_review and file.approval_status != "approved":
        pending = replace(file, approval_status="pending", index_status="awaiting_review")
        store.update_file(pending, expected_generation=file.generation)
        return {
            "status": "awaiting_review",
            "file_id": file.id,
            "generation": file.generation,
            "pii_types": sorted({row["type"] for row in findings}),
        }

    pieces = chunk_text(text)
    if not pieces:
        raise ValueError("No extractable text in Drive file.")
    if len(pieces) > MAX_DRIVE_CHUNKS:
        raise ValueError("Drive file exceeds the configured chunk limit.")
    file = _set_state(store, file, "indexing")
    embedder = get_embedder()
    vectors = []
    for offset in range(0, len(pieces), EMBED_BATCH_SIZE):
        batch = pieces[offset: offset + EMBED_BATCH_SIZE]
        vectors.extend(embedder.embed([f"{file.name}. {piece}" for piece in batch]))
    if len(vectors) != len(pieces):
        raise RuntimeError("Embedding provider returned an incomplete Drive batch.")

    doc_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"onebrain:drive:{file.id}:{revision.id}:{file.generation}",
    ).hex
    created_at = datetime.now(timezone.utc).isoformat()
    chunks = []
    for index, (piece, vector) in enumerate(zip(pieces, vectors)):
        meta = {
            "tenant_id": file.tenant_id,
            "account_id": file.account_id,
            "space_id": file.space_id,
            "space_kind": file.space_kind,
            "owner_user_id": file.owner_user_id,
            "doc_title": file.name,
            "classification": int(classification),
            "classification_label": classification.name.lower(),
            "location": file.location,
            "category": file.category,
            "chunk_index": index,
            "uploaded_by": file.uploaded_by,
            "status": STATUS_APPROVED,
            "pii_findings": findings,
            "created_at": created_at,
            "drive_file_id": file.id,
            "drive_revision_id": revision.id,
            "drive_generation": file.generation,
        }
        chunks.append(Chunk(
            id=f"{doc_id}:{index}", doc_id=doc_id, text=piece, meta=meta, embedding=vector,
        ))
    published = store.publish_projection(
        file_id=file.id,
        revision_id=revision.id,
        generation=file.generation,
        account_id=file.account_id,
        space_id=file.space_id,
        chunks=chunks,
    )
    return {
        "status": "indexed",
        "file_id": file.id,
        "doc_id": published.file.active_doc_id,
        "generation": published.file.generation,
        "chunks": published.chunks,
        "pii_types": sorted({row["type"] for row in findings}),
    }


def _set_state(store, file, state: str):
    return store.update_file(replace(file, index_status=state), expected_generation=file.generation)


def mark_drive_job_failed(job) -> None:
    """Best-effort visible failure state; generation fencing prevents stale jobs mutating files."""

    from app.deps import get_drive_store

    payload = job.payload
    try:
        generation = int(payload.get("generation"))
    except (TypeError, ValueError):
        return
    file_id = str(payload.get("file_id") or "")
    if not file_id:
        return
    store = get_drive_store()
    file = store.get_file(file_id, account_id=job.account_id, space_id=job.space_id)
    if (
        not file or file.generation != generation or file.trashed_at
        or not file.desired_indexed or file.index_status == "indexed"
    ):
        return
    try:
        store.update_file(replace(file, index_status="failed"), expected_generation=file.generation)
    except (DriveGenerationConflict, KeyError):
        return

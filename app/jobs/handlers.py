"""Job handler dispatch for ingestion-related jobs."""

from __future__ import annotations

from app.intake.pipeline import IntakeInput
from app.jobs.base import (
    JOB_DOCUMENT_INGEST,
    JOB_DRIVE_FILE_INGEST,
    JOB_DRIVE_REVISION_MALWARE_SCAN,
    JOB_RETENTION_RUN,
    JOB_SERVICE_CAPTURE,
    JOB_SERVICE_INTAKE,
    Job,
    JobStore,
)
from app.security.policy import CAPTURED_CATEGORY


def _intake_record(record) -> dict:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "account_id": record.account_id,
        "space_id": record.space_id,
        "app_id": record.app_id,
        "purpose": record.purpose,
        "source": record.source,
        "source_ref": record.source_ref,
        "record_type": record.record_type,
        "intent": record.intent,
        "classification": record.classification,
        "confidence": record.confidence,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "extracted_facts": record.extracted_facts,
        "metadata": record.metadata,
        "created_at": record.created_at,
    }


def handle_job(job: Job, store: JobStore) -> dict:
    if job.type == JOB_DOCUMENT_INGEST:
        # These payloads predate Drive's revision-scoped malware attestation.
        # Failing permanently also purges terminal job_file bytes, so a queued
        # pre-upgrade upload cannot become an extraction/indexing bypass.
        raise ValueError("Legacy document ingestion is retired; upload through OneBrain Drive.")
    if job.type == JOB_DRIVE_FILE_INGEST:
        from app.drive.indexing import handle_drive_index_job, mark_drive_job_failed

        try:
            return handle_drive_index_job(job)
        except Exception:
            try:
                mark_drive_job_failed(job)
            except Exception:
                pass
            raise
    if job.type == JOB_DRIVE_REVISION_MALWARE_SCAN:
        from app.deps import get_drive_malware_scanning_service

        return get_drive_malware_scanning_service().handle(job)
    if job.type == JOB_SERVICE_CAPTURE:
        return _handle_service_capture(job)
    if job.type == JOB_SERVICE_INTAKE:
        return _handle_service_intake(job)
    if job.type == JOB_RETENTION_RUN:
        return _handle_retention_run(job)
    raise ValueError(f"unknown job type: {job.type}")


def _handle_service_capture(job: Job) -> dict:
    from app.deps import get_pipeline

    payload = job.payload
    result = get_pipeline().ingest_text(
        title=payload.get("title") or "captured message",
        text=payload.get("text") or "",
        classification="internal",
        location="global",
        category=CAPTURED_CATEGORY,
        uploaded_by=job.requested_by,
        tenant=job.tenant_id,
        require_approval=False,
        block_public_on_pii=False,
        pii_phase=payload.get("pii_phase", "dpia_signed"),
        account_id=job.account_id,
        space_id=job.space_id,
        idempotency_key=job.id,
    )
    return {"captured": result.doc_id, "chunks": result.chunks}


def _handle_service_intake(job: Job) -> dict:
    from app.deps import get_intake_pipeline

    payload = job.payload
    record = get_intake_pipeline().ingest(IntakeInput(
        tenant_id=job.tenant_id,
        account_id=job.account_id,
        space_id=job.space_id,
        app_id=payload.get("app_id", ""),
        purpose=payload.get("purpose", ""),
        content=payload.get("content", ""),
        title=payload.get("title", ""),
        source=payload.get("source") or "service",
        source_ref=payload.get("source_ref", ""),
        record_type=payload.get("record_type", ""),
        intent=payload.get("intent", ""),
        metadata=payload.get("metadata") or {},
        idempotency_key=job.id,
    ))
    return {"record": _intake_record(record)}


def _handle_retention_run(job: Job) -> dict:
    from app.retention.service import run_retention

    payload = job.payload
    return run_retention(
        account_id=job.account_id or payload.get("account_id") or job.tenant_id,
        space_id=job.space_id or payload.get("space_id", ""),
        domain=payload.get("domain", ""),
        dry_run=bool(payload.get("dry_run", True)),
    )

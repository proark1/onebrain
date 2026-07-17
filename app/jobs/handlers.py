"""Job handler dispatch for ingestion-related jobs."""

from __future__ import annotations

from app.intake.pipeline import IntakeInput
from app.jobs.base import JOB_DOCUMENT_INGEST, JOB_RETENTION_RUN, JOB_SERVICE_CAPTURE, JOB_SERVICE_INTAKE, Job, JobStore
from app.security.policy import CAPTURED_CATEGORY


def _document_summary(result, account_id: str = "", space_id: str = "") -> dict:
    return {
        "doc_id": result.doc_id,
        "title": result.title,
        "classification": result.classification,
        "location": result.location,
        "category": result.category,
        "chunks": result.chunks,
        "status": result.status,
        "pii_findings": len(result.pii_findings),
        "account_id": account_id,
        "space_id": space_id,
    }


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
        return _handle_document_ingest(job, store)
    if job.type == JOB_SERVICE_CAPTURE:
        return _handle_service_capture(job)
    if job.type == JOB_SERVICE_INTAKE:
        return _handle_service_intake(job)
    if job.type == JOB_RETENTION_RUN:
        return _handle_retention_run(job)
    raise ValueError(f"unknown job type: {job.type}")


def _handle_document_ingest(job: Job, store: JobStore) -> dict:
    from app.deps import get_pipeline

    file = store.get_file(job.id)
    if file is None:
        raise ValueError("document ingestion job is missing its file payload")
    payload = job.payload
    result = get_pipeline().ingest_file(
        filename=file.filename,
        data=file.data,
        classification=payload.get("classification", "internal"),
        location=payload.get("location", "global"),
        category=payload.get("category", "general"),
        uploaded_by=job.requested_by,
        tenant=job.tenant_id,
        require_approval=bool(payload.get("require_approval", False)),
        block_public_on_pii=bool(payload.get("block_public_on_pii", True)),
        pii_phase=payload.get("pii_phase", "dpia_signed"),
        account_id=job.account_id,
        space_id=job.space_id,
        idempotency_key=job.id,
    )
    return _document_summary(result, job.account_id, job.space_id)


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

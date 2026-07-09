from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.deps
import app.routers.jobs as jobs_router
import app.routers.service as service_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.embeddings.local import LocalEmbedder
from app.ingest.pipeline import IngestPipeline
from app.intake.memory import MemoryIntakeStore
from app.intake.pipeline import IntakeInput, IntakePipeline
from app.jobs.base import (
    JOB_DOCUMENT_INGEST,
    JOB_RETENTION_RUN,
    JOB_SERVICE_CAPTURE,
    JOB_SERVICE_INTAKE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_QUEUED,
    JobFileInput,
)
from app.jobs.memory import MemoryJobStore
from app.platform.base import Account, AppInstallation, RetentionPolicy, Space
from app.platform.memory import MemoryPlatformStore
from app.schemas import ServiceCaptureRequest, ServiceIntakeRequest
from app.security.policy import Classification
from app.servicekeys.base import SCOPE_WRITE
from app.store.memory import MemoryStore
from app.workers.service import Worker


def _human(role_id: str = "admin", tenant: str = "nft_gym") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@nft_gym",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"munich"}),
        categories=role.categories,
        location_label="all",
        tenant_id=tenant,
    )


def _service(scopes=(SCOPE_WRITE,), tenant="nft_gym") -> Principal:
    return Principal(
        user_id="svc:key",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,
        locations=frozenset(),
        categories=frozenset({"general"}),
        location_label="-",
        tenant_id=tenant,
        principal_type="service",
        scopes=frozenset(scopes),
    )


def _platform_store() -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="nft_gym", kind="organization", name="NFT Gym"))
    platform.create_space(Space(id="sp_customer", account_id="nft_gym", kind="customer_service", name="Customer"))
    platform.install_app(AppInstallation(
        id="appi_comm",
        account_id="nft_gym",
        app_id="communication",
        enabled_space_ids=("sp_customer",),
        allowed_purposes=("customer_service_inbox",),
    ))
    return platform


def test_memory_job_store_lifecycle_with_file_payload():
    store = MemoryJobStore()
    job = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="nft_gym",
        payload={"classification": "public"},
        file=JobFileInput("hello.txt", "text/plain", b"hello"),
    )

    assert store.get(job.id).status == "queued"
    assert store.get_file(job.id).data == b"hello"

    claimed = store.claim("worker_a")

    assert len(claimed) == 1
    assert claimed[0].status == STATUS_RUNNING
    assert claimed[0].attempts == 1

    done = store.mark_succeeded(job.id, {"ok": True})

    assert done.status == STATUS_SUCCEEDED
    assert done.result == {"ok": True}
    assert done.completed_at


def test_memory_job_store_summary_is_sanitized():
    store = MemoryJobStore()
    failed = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        payload={"raw": "not returned"},
        file=JobFileInput("secret.txt", "text/plain", b"not returned"),
    )
    queued = store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="nft_gym")
    store.claim("worker_a")
    store.mark_failed(failed.id, "provider timeout")

    summary = store.summary(recent_failures_limit=1)

    assert summary.total == 2
    assert summary.by_status[STATUS_FAILED] == 1
    assert summary.by_status[STATUS_QUEUED] == 1
    assert summary.by_type[JOB_DOCUMENT_INGEST] == 1
    assert summary.by_type[JOB_SERVICE_CAPTURE] == 1
    assert summary.recent_failures[0].id == failed.id
    assert summary.recent_failures[0].error == "provider timeout"
    assert not hasattr(summary.recent_failures[0], "payload")
    assert not hasattr(summary.recent_failures[0], "result")
    assert store.get_file(failed.id).data == b"not returned"
    assert store.get(queued.id).status == STATUS_QUEUED


def test_worker_processes_document_ingest_job(monkeypatch):
    vector_store = MemoryStore()
    pipeline = IngestPipeline(LocalEmbedder(), vector_store)
    job_store = MemoryJobStore()
    monkeypatch.setattr(app.deps, "get_pipeline", lambda: pipeline)

    job_store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        requested_by="uploader",
        payload={
            "classification": "public",
            "location": "global",
            "category": "general",
            "pii_phase": "dpia_signed",
        },
        file=JobFileInput("policy.txt", "text/plain", b"Support is open from 9 to 6."),
    )

    worker = Worker(job_store, worker_id="worker_test")
    assert worker.run_once() == 1

    job = next(iter(job_store._jobs.values()))
    assert job.status == STATUS_SUCCEEDED
    assert job.result["chunks"] == 1
    assert vector_store.count() == 1


def test_worker_processes_service_capture_job(monkeypatch):
    vector_store = MemoryStore()
    pipeline = IngestPipeline(LocalEmbedder(), vector_store)
    job_store = MemoryJobStore()
    monkeypatch.setattr(app.deps, "get_pipeline", lambda: pipeline)

    job_store.enqueue(
        type=JOB_SERVICE_CAPTURE,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        requested_by="svc:key",
        payload={"text": "A customer asked about memberships.", "pii_phase": "dpia_signed"},
    )

    Worker(job_store, worker_id="worker_test").run_once()

    job = next(iter(job_store._jobs.values()))
    assert job.status == STATUS_SUCCEEDED
    assert job.result["chunks"] == 1
    assert vector_store.count() == 1


def test_worker_processes_service_intake_job(monkeypatch):
    intake_store = MemoryIntakeStore()
    settings = SimpleNamespace(pii_phase="dpia_signed", require_approval=False)
    pipeline = IntakePipeline(intake_store, settings)
    job_store = MemoryJobStore()
    monkeypatch.setattr(app.deps, "get_intake_pipeline", lambda: pipeline)

    job_store.enqueue(
        type=JOB_SERVICE_INTAKE,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        requested_by="svc:key",
        payload={
            "app_id": "communication",
            "purpose": "customer_service_inbox",
            "content": "Customer wants to reschedule a booking.",
            "source": "communication",
        },
    )

    Worker(job_store, worker_id="worker_test").run_once()

    job = next(iter(job_store._jobs.values()))
    assert job.status == STATUS_SUCCEEDED
    assert job.result["record"]["intent"] == "booking"
    assert intake_store.count() == 1


def test_worker_processes_retention_run_with_dry_run_and_enforcement(monkeypatch):
    platform = _platform_store()
    platform.upsert_retention_policy(RetentionPolicy(
        id="ret_intake",
        account_id="nft_gym",
        space_id="sp_customer",
        domain="intake",
        record_type="message",
        action="delete",
        duration_days=0,
        legal_basis="test policy",
    ))
    intake_store = MemoryIntakeStore()
    settings = SimpleNamespace(pii_phase="dpia_signed", require_approval=False)
    IntakePipeline(intake_store, settings).ingest(IntakeInput(
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        app_id="communication",
        purpose="customer_service_inbox",
        content="Customer retention sample.",
        source="communication",
    ))
    job_store = MemoryJobStore()
    monkeypatch.setattr(app.deps, "get_platform_store", lambda: platform)
    monkeypatch.setattr(app.deps, "get_intake_store", lambda: intake_store)

    dry = job_store.enqueue(
        type=JOB_RETENTION_RUN,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        payload={"domain": "intake", "dry_run": True},
    )
    Worker(job_store, worker_id="worker_test").run_once()
    assert job_store.get(dry.id).result["counts"]["intake_records"] == 1
    assert intake_store.count() == 1

    enforce = job_store.enqueue(
        type=JOB_RETENTION_RUN,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        payload={"domain": "intake", "dry_run": False},
    )
    Worker(job_store, worker_id="worker_test").run_once()
    assert job_store.get(enforce.id).result["counts"]["intake_records_deleted"] == 1
    assert intake_store.count() == 0


def test_worker_marks_permanent_validation_failures_failed(monkeypatch):
    job_store = MemoryJobStore()
    job_store.enqueue(type="unknown", tenant_id="nft_gym")

    Worker(job_store, worker_id="worker_test").run_once()

    job = next(iter(job_store._jobs.values()))
    assert job.status == STATUS_FAILED
    assert "unknown job type" in job.error


def test_job_status_endpoint_enforces_tenant_and_space(monkeypatch):
    job_store = MemoryJobStore()
    job = job_store.enqueue(
        type=JOB_SERVICE_INTAKE,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        requested_by="svc:key",
    )
    monkeypatch.setattr(jobs_router, "get_job_store", lambda: job_store)
    monkeypatch.setattr(jobs_router, "get_platform_store", _platform_store)

    visible = jobs_router.get_job(job.id, principal=_human("admin"))

    assert visible.id == job.id

    with pytest.raises(HTTPException) as exc:
        jobs_router.get_job(job.id, principal=_human("admin", tenant="other"))
    assert exc.value.status_code == 404


def test_service_async_capture_enqueues_job(monkeypatch):
    job_store = MemoryJobStore()
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_job_store", lambda: job_store)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(
        service_router,
        "get_settings",
        lambda: SimpleNamespace(use_async_ingestion=True, pii_phase="dpia_signed", job_max_attempts=3),
    )

    result = service_router.capture(
        ServiceCaptureRequest(
            text="Synthetic customer message.",
            account_id="nft_gym",
            space_id="sp_customer",
            app_id="communication",
        ),
        principal=_service(),
    )

    assert result.status == "queued"
    assert job_store.get(result.id).type == JOB_SERVICE_CAPTURE
    assert job_store.get(result.id).payload["text"] == "Synthetic customer message."


def test_service_async_intake_enqueues_job(monkeypatch):
    job_store = MemoryJobStore()
    platform = _platform_store()
    monkeypatch.setattr(service_router, "get_job_store", lambda: job_store)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(
        service_router,
        "get_settings",
        lambda: SimpleNamespace(use_async_ingestion=True, pii_phase="dpia_signed", job_max_attempts=3),
    )
    principal = replace(
        _service(),
        account_id="nft_gym",
        app_id="communication",
        space_ids=frozenset({"sp_customer"}),
        purposes=frozenset({"customer_service_inbox"}),
    )

    result = service_router.intake(
        ServiceIntakeRequest(content="Customer wants to reschedule.", source="communication"),
        principal=principal,
    )

    assert result.status == "queued"
    assert job_store.get(result.id).type == JOB_SERVICE_INTAKE
    assert job_store.get(result.id).payload["app_id"] == "communication"

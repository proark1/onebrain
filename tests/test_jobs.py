from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.deps
import app.routers.jobs as jobs_router
import app.routers.service as service_router
import app.workers.service as worker_service
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
    LEASE_EXPIRED_ERROR,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_QUEUED,
    JobLeaseLostError,
    JobFileInput,
    utcnow,
)
from app.jobs.handlers import handle_job
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

    done = store.mark_succeeded(job.id, {"ok": True}, lease_token=claimed[0].lease_token)

    assert done.status == STATUS_SUCCEEDED
    assert done.result == {"ok": True}
    assert done.completed_at
    assert store.get_file(job.id) is None


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
    claimed = store.claim("worker_a")
    store.mark_failed(failed.id, "provider timeout", lease_token=claimed[0].lease_token)

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
    assert store.get_file(failed.id) is None
    assert store.get(queued.id).status == STATUS_QUEUED


def test_memory_job_store_retry_keeps_file_until_terminal_result():
    store = MemoryJobStore()
    job = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="nft_gym",
        file=JobFileInput("retry.txt", "text/plain", b"retry me"),
        max_attempts=2,
    )
    first = store.claim("worker_a")[0]

    store.mark_retry(
        job.id,
        "temporary failure",
        utcnow() - timedelta(seconds=1),
        lease_token=first.lease_token,
    )

    assert store.get_file(job.id).data == b"retry me"
    second = store.claim("worker_b")[0]
    store.mark_failed(job.id, "final failure", lease_token=second.lease_token)
    assert store.get_file(job.id) is None


def test_memory_job_store_reclaims_expired_lease_with_a_new_fencing_token():
    store = MemoryJobStore()
    job = store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="nft_gym", max_attempts=2)
    first = store.claim("worker_a", lease_seconds=60)[0]
    store._jobs[job.id] = replace(
        first,
        lease_expires_at=(utcnow() - timedelta(seconds=1)).isoformat(),
    )

    second = store.claim("worker_b", lease_seconds=60)[0]

    assert second.status == STATUS_RUNNING
    assert second.attempts == 2
    assert second.lease_token
    assert second.lease_token != first.lease_token
    assert second.lease_expires_at > utcnow().isoformat()


def test_memory_job_store_fences_stale_lease_updates_and_heartbeat():
    store = MemoryJobStore()
    job = store.enqueue(
        type=JOB_SERVICE_CAPTURE,
        tenant_id="nft_gym",
        file=JobFileInput("fenced.txt", "text/plain", b"keep until owner finishes"),
        max_attempts=2,
    )
    first = store.claim("worker_a")[0]
    store._jobs[job.id] = replace(
        first,
        lease_expires_at=(utcnow() - timedelta(seconds=1)).isoformat(),
    )
    second = store.claim("worker_b")[0]

    with pytest.raises(JobLeaseLostError):
        store.renew_lease(job.id, first.lease_token, 60)
    with pytest.raises(JobLeaseLostError):
        store.mark_succeeded(job.id, {"stale": True}, lease_token=first.lease_token)
    with pytest.raises(JobLeaseLostError):
        store.mark_failed(job.id, "stale", lease_token=first.lease_token)
    with pytest.raises(JobLeaseLostError):
        store.mark_retry(
            job.id,
            "stale",
            utcnow() + timedelta(seconds=1),
            lease_token=first.lease_token,
        )
    assert store.get_file(job.id).data == b"keep until owner finishes"

    done = store.mark_succeeded(job.id, {"ok": True}, lease_token=second.lease_token)
    assert done.status == STATUS_SUCCEEDED
    assert store.get_file(job.id) is None


def test_memory_job_store_terminalizes_expired_last_attempt_without_reclaiming():
    store = MemoryJobStore()
    job = store.enqueue(
        type=JOB_SERVICE_CAPTURE,
        tenant_id="nft_gym",
        file=JobFileInput("expired.txt", "text/plain", b"expired"),
        max_attempts=1,
    )
    claimed = store.claim("worker_a")[0]
    store._jobs[job.id] = replace(
        claimed,
        lease_expires_at=(utcnow() - timedelta(seconds=1)).isoformat(),
    )

    assert store.claim("worker_b") == []
    failed = store.get(job.id)
    assert failed.status == STATUS_FAILED
    assert failed.error == LEASE_EXPIRED_ERROR
    assert failed.completed_at
    assert failed.lease_token == ""
    assert failed.lease_expires_at == ""
    assert store.get_file(job.id) is None


def test_memory_job_store_delete_scope_is_account_and_space_bound():
    store = MemoryJobStore()
    service = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="acme",
        account_id="acme",
        space_id="sp_service",
        file=JobFileInput("service.txt", "text/plain", b"service"),
    )
    personal = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="acme",
        account_id="acme",
        space_id="sp_personal",
        file=JobFileInput("personal.txt", "text/plain", b"personal"),
    )
    legacy = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="acme",
        file=JobFileInput("legacy.txt", "text/plain", b"legacy"),
    )
    other = store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="other",
        account_id="other",
        space_id="sp_service",
        file=JobFileInput("other.txt", "text/plain", b"other"),
    )

    deleted_space = store.delete_scope(
        "acme", account_id="acme", space_id="sp_service",
    )

    assert (deleted_space.jobs, deleted_space.files) == (1, 1)
    assert store.get(service.id) is None
    assert store.get(personal.id) is not None
    assert store.get(legacy.id) is not None
    assert store.get(other.id) is not None

    deleted_account = store.delete_scope("acme", account_id="acme")

    assert (deleted_account.jobs, deleted_account.files) == (2, 2)
    assert store.get(personal.id) is None
    assert store.get(legacy.id) is None
    assert store.get(other.id) is not None


def test_worker_heartbeats_active_job_lease(monkeypatch):
    class ObservingStore(MemoryJobStore):
        def __init__(self):
            super().__init__()
            self.renewals = []

        def renew_lease(self, job_id, lease_token, lease_seconds):
            self.renewals.append((job_id, lease_token, lease_seconds))
            return super().renew_lease(job_id, lease_token, lease_seconds)

    store = ObservingStore()
    store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="nft_gym")
    monkeypatch.setattr(
        worker_service,
        "handle_job",
        lambda *_args: (time.sleep(0.08), {"ok": True})[1],
    )
    worker = Worker(store, worker_id="worker_test")
    worker.settings = SimpleNamespace(
        worker_batch_size=1,
        job_lease_seconds=1,
        job_lease_heartbeat_seconds=0.01,
    )

    assert worker.run_once() == 1
    assert store.renewals
    assert store.get(next(iter(store._jobs))).status == STATUS_SUCCEEDED


def test_worker_does_not_prefetch_leases_for_sequential_batch_work(monkeypatch):
    class ObservingStore(MemoryJobStore):
        def __init__(self):
            super().__init__()
            self.claim_limits: list[int] = []

        def claim(self, worker_id, limit=1, lease_seconds=60):
            self.claim_limits.append(limit)
            return super().claim(worker_id, limit=limit, lease_seconds=lease_seconds)

    store = ObservingStore()
    first = store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="nft_gym")
    second = store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="nft_gym")
    handled: list[str] = []

    def slow_handler(job, _store):
        handled.append(job.id)
        # The later job remains queued while this one runs; it has no lease to
        # expire and therefore cannot be reclaimed by another worker.
        if job.id == first.id:
            assert store.get(second.id).status == STATUS_QUEUED
        return {"ok": True}

    monkeypatch.setattr(worker_service, "handle_job", slow_handler)
    worker = Worker(store, worker_id="worker_test")
    worker.settings = SimpleNamespace(
        worker_batch_size=2,
        job_lease_seconds=1,
        job_lease_heartbeat_seconds=0.01,
    )

    assert worker.run_once() == 2
    assert handled == [first.id, second.id]
    assert store.claim_limits == [1, 1]
    assert store.get(first.id).status == STATUS_SUCCEEDED
    assert store.get(second.id).status == STATUS_SUCCEEDED


def test_worker_shutdown_stops_new_claims():
    store = MemoryJobStore()
    job = store.enqueue(type=JOB_SERVICE_CAPTURE, tenant_id="nft_gym")
    worker = Worker(store, worker_id="worker_test")

    worker.stop_claiming()

    assert worker.run_once() == 0
    assert store.get(job.id).status == STATUS_QUEUED


def test_ingestion_job_handlers_reuse_outputs_after_a_recovered_attempt(monkeypatch):
    vector_store = MemoryStore()
    pipeline = IngestPipeline(LocalEmbedder(), vector_store)
    job_store = MemoryJobStore()
    monkeypatch.setattr(app.deps, "get_pipeline", lambda: pipeline)
    document = job_store.enqueue(
        type=JOB_DOCUMENT_INGEST,
        tenant_id="nft_gym",
        payload={"classification": "internal"},
        file=JobFileInput("policy.txt", "text/plain", b"Support is open from 9 to 6."),
    )
    capture = job_store.enqueue(
        type=JOB_SERVICE_CAPTURE,
        tenant_id="nft_gym",
        payload={"text": "A customer asked about memberships."},
    )

    document_first = handle_job(document, job_store)
    document_second = handle_job(document, job_store)
    capture_first = handle_job(capture, job_store)
    capture_second = handle_job(capture, job_store)

    assert document_second == document_first
    assert capture_second == capture_first
    assert vector_store.count() == document_first["chunks"] + capture_first["chunks"]


def test_intake_job_handler_reuses_output_after_a_recovered_attempt(monkeypatch):
    intake_store = MemoryIntakeStore()
    pipeline = IntakePipeline(
        intake_store,
        SimpleNamespace(pii_phase="dpia_signed", require_approval=False),
    )
    job_store = MemoryJobStore()
    monkeypatch.setattr(app.deps, "get_intake_pipeline", lambda: pipeline)
    job = job_store.enqueue(
        type=JOB_SERVICE_INTAKE,
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        payload={
            "app_id": "communication",
            "purpose": "customer_service_inbox",
            "content": "Customer wants to reschedule a booking.",
            "source": "communication",
        },
    )

    first = handle_job(job, job_store)
    second = handle_job(job, job_store)

    assert second == first
    assert intake_store.count() == 1


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

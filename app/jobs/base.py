"""Shared job types and store contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional, Protocol, Sequence


JOB_DOCUMENT_INGEST = "document_ingest"
JOB_SERVICE_CAPTURE = "service_capture"
JOB_SERVICE_INTAKE = "service_intake"
JOB_RETENTION_RUN = "retention_run"
JOB_DRIVE_FILE_INGEST = "drive_file_ingest"
JOB_DRIVE_REVISION_MALWARE_SCAN = "drive_revision_malware_scan"
JOB_ACCOUNTING_EXTRACT = "accounting_extract"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_RETRYING = "retrying"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

READY_STATUSES = (STATUS_QUEUED, STATUS_RETRYING)
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)
LEASE_EXPIRED_ERROR = "job lease expired before completion"
MAX_JOB_ENQUEUE_BATCH = 250


class JobLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns a job's active lease."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class JobFileInput:
    filename: str
    content_type: str
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class JobFile:
    id: str
    job_id: str
    filename: str
    content_type: str
    size_bytes: int
    data: bytes
    created_at: str = ""


@dataclass(frozen=True)
class Job:
    id: str
    type: str
    status: str
    tenant_id: str
    account_id: str = ""
    space_id: str = ""
    requested_by: str = ""
    payload: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error: str = ""
    attempts: int = 0
    max_attempts: int = 3
    run_after: str = ""
    locked_by: str = ""
    locked_at: str = ""
    lease_token: str = ""
    lease_expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


@dataclass(frozen=True)
class JobEnqueueSpec:
    """One idempotent, byte-free unit of work for a bounded batch enqueue."""

    type: str
    tenant_id: str
    account_id: str = ""
    space_id: str = ""
    requested_by: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)
    max_attempts: int = 3
    idempotency_key: str = ""
    job_id: str = ""


def validate_job_enqueue_batch(
    specs: Sequence[JobEnqueueSpec],
) -> tuple[JobEnqueueSpec, ...]:
    """Validate the deliberately narrow contract shared by queue backends.

    A batch is one RLS scope and every item must be idempotent. Uploaded bytes
    stay on the scalar enqueue path because they have a separate lifecycle.
    """

    batch = tuple(specs)
    if len(batch) > MAX_JOB_ENQUEUE_BATCH:
        raise ValueError(
            f"Job enqueue batch cannot exceed {MAX_JOB_ENQUEUE_BATCH} items."
        )
    if not batch:
        return batch
    scope = (batch[0].tenant_id, batch[0].account_id, batch[0].space_id)
    for spec in batch:
        if (spec.tenant_id, spec.account_id, spec.space_id) != scope:
            raise ValueError("Job enqueue batch must use one tenant/account/space scope.")
        if not (spec.type or "").strip():
            raise ValueError("Job type is required.")
        if not (spec.tenant_id or "").strip():
            raise ValueError("Job tenant id is required.")
        dedupe = (spec.idempotency_key or "").strip()
        if not dedupe:
            raise ValueError("Batch-enqueued jobs require an idempotency key.")
        if len(dedupe) > 200:
            raise ValueError("Job idempotency key is too long.")
        explicit_job_id = (spec.job_id or "").strip()
        if explicit_job_id and (
            not explicit_job_id.startswith("job_") or len(explicit_job_id) > 128
        ):
            raise ValueError("Explicit job id must be an opaque job_ identifier.")
        if int(spec.max_attempts) < 1:
            raise ValueError("Job max attempts must be at least one.")
        if not isinstance(spec.payload, Mapping):
            raise ValueError("Job payload must be a mapping.")
    return batch


@dataclass(frozen=True)
class JobFailureSummary:
    id: str
    type: str
    tenant_id: str
    account_id: str = ""
    space_id: str = ""
    attempts: int = 0
    max_attempts: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


@dataclass(frozen=True)
class JobSummary:
    total: int
    by_status: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    recent_failures: list[JobFailureSummary] = field(default_factory=list)


@dataclass(frozen=True)
class JobScopeDeleteResult:
    """Content-free counts returned after deleting a privacy scope's jobs."""

    jobs: int = 0
    files: int = 0


class JobStore(Protocol):
    def enqueue(
        self,
        *,
        job_id: str = "",
        type: str,
        tenant_id: str,
        account_id: str = "",
        space_id: str = "",
        requested_by: str = "",
        payload: Optional[dict] = None,
        file: Optional[JobFileInput] = None,
        max_attempts: int = 3,
        idempotency_key: str = "",
    ) -> Job: ...

    def enqueue_many(
        self,
        specs: Sequence[JobEnqueueSpec],
    ) -> tuple[Job, ...]: ...

    def get(
        self,
        job_id: str,
        *,
        tenant_id: str = "",
        account_id: str = "",
        space_id: str = "",
    ) -> Optional[Job]: ...

    def get_file(self, job_id: str) -> Optional[JobFile]: ...

    def claim(self, worker_id: str, limit: int = 1, lease_seconds: int = 60) -> list[Job]: ...

    def renew_lease(self, job_id: str, lease_token: str, lease_seconds: int) -> Job: ...

    def mark_succeeded(self, job_id: str, result: dict, *, lease_token: str) -> Job: ...

    def mark_failed(self, job_id: str, error: str, *, lease_token: str) -> Job: ...

    def mark_retry(
        self,
        job_id: str,
        error: str,
        run_after: datetime,
        *,
        lease_token: str,
    ) -> Job: ...

    def delete_scope(
        self,
        tenant_id: str,
        *,
        account_id: str = "",
        space_id: str = "",
    ) -> JobScopeDeleteResult: ...

    def summary(self, recent_failures_limit: int = 10) -> JobSummary: ...

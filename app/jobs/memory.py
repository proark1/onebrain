"""In-process job store for local mode and tests."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from app.jobs.base import (
    JobFailureSummary,
    JobSummary,
    READY_STATUSES,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RETRYING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    Job,
    JobFile,
    JobFileInput,
    utcnow,
)


KNOWN_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED)


def _iso(value: datetime | str | None = None) -> str:
    if value is None:
        value = utcnow()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class MemoryJobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._files: dict[str, JobFile] = {}
        self._lock = threading.RLock()

    def enqueue(
        self,
        *,
        type: str,
        tenant_id: str,
        account_id: str = "",
        space_id: str = "",
        requested_by: str = "",
        payload: dict | None = None,
        file: JobFileInput | None = None,
        max_attempts: int = 3,
    ) -> Job:
        now = _iso()
        job = Job(
            id=f"job_{uuid4().hex}",
            type=type,
            status=STATUS_QUEUED,
            tenant_id=tenant_id,
            account_id=account_id,
            space_id=space_id,
            requested_by=requested_by,
            payload=dict(payload or {}),
            max_attempts=max_attempts,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._jobs[job.id] = job
            if file is not None:
                self._files[job.id] = JobFile(
                    id=f"file_{uuid4().hex}",
                    job_id=job.id,
                    filename=file.filename,
                    content_type=file.content_type,
                    size_bytes=file.size_bytes,
                    data=file.data,
                    created_at=now,
                )
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_file(self, job_id: str) -> JobFile | None:
        with self._lock:
            return self._files.get(job_id)

    def claim(self, worker_id: str, limit: int = 1) -> list[Job]:
        now = utcnow()
        out: list[Job] = []
        with self._lock:
            ready = sorted(
                (
                    job for job in self._jobs.values()
                    if job.status in READY_STATUSES and _parse(job.run_after) <= now
                ),
                key=lambda job: job.created_at,
            )
            for job in ready[:max(1, limit)]:
                updated = replace(
                    job,
                    status=STATUS_RUNNING,
                    attempts=job.attempts + 1,
                    locked_by=worker_id,
                    locked_at=_iso(now),
                    updated_at=_iso(now),
                )
                self._jobs[job.id] = updated
                out.append(updated)
        return out

    def mark_succeeded(self, job_id: str, result: dict) -> Job:
        return self._update_terminal(job_id, STATUS_SUCCEEDED, result=result)

    def mark_failed(self, job_id: str, error: str) -> Job:
        return self._update_terminal(job_id, STATUS_FAILED, error=error)

    def mark_retry(self, job_id: str, error: str, run_after: datetime) -> Job:
        with self._lock:
            job = self._require(job_id)
            updated = replace(
                job,
                status=STATUS_RETRYING,
                error=error[:2000],
                run_after=_iso(run_after),
                locked_by="",
                locked_at="",
                updated_at=_iso(),
            )
            self._jobs[job_id] = updated
            return updated

    def summary(self, recent_failures_limit: int = 10) -> JobSummary:
        with self._lock:
            jobs = list(self._jobs.values())
        by_status = {status: 0 for status in KNOWN_STATUSES}
        by_type: dict[str, int] = {}
        for job in jobs:
            by_status[job.status] = by_status.get(job.status, 0) + 1
            by_type[job.type] = by_type.get(job.type, 0) + 1
        failed = sorted(
            (job for job in jobs if job.status == STATUS_FAILED),
            key=lambda job: job.completed_at or job.updated_at or job.created_at or job.id,
            reverse=True,
        )
        limit = max(0, recent_failures_limit)
        return JobSummary(
            total=len(jobs),
            by_status=by_status,
            by_type=by_type,
            recent_failures=[
                JobFailureSummary(
                    id=job.id,
                    type=job.type,
                    tenant_id=job.tenant_id,
                    account_id=job.account_id,
                    space_id=job.space_id,
                    attempts=job.attempts,
                    max_attempts=job.max_attempts,
                    error=job.error[:500],
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    completed_at=job.completed_at,
                )
                for job in failed[:limit]
            ],
        )

    def _update_terminal(self, job_id: str, status: str, result: dict | None = None, error: str = "") -> Job:
        with self._lock:
            job = self._require(job_id)
            now = _iso()
            updated = replace(
                job,
                status=status,
                result=result,
                error=error[:2000],
                locked_by="",
                locked_at="",
                updated_at=now,
                completed_at=now,
            )
            self._jobs[job_id] = updated
            return updated

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

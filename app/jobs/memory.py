"""In-process job store for local mode and tests."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Sequence
from uuid import uuid4

from app.jobs.base import (
    JobEnqueueSpec,
    JobFailureSummary,
    JobScopeDeleteResult,
    JobSummary,
    JobLeaseLostError,
    LEASE_EXPIRED_ERROR,
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
    validate_job_enqueue_batch,
)


KNOWN_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED)


def _iso(value: datetime | str | None = None) -> str:
    if value is None:
        value = utcnow()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _lease_expired(job: Job, now: datetime) -> bool:
    if not job.lease_expires_at:
        # Running rows created before leases existed are intentionally reclaimable.
        return True
    try:
        return _parse(job.lease_expires_at) <= now
    except (TypeError, ValueError):
        return True


class MemoryJobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._files: dict[str, JobFile] = {}
        self._idempotency: dict[tuple[str, str, str, str, str], str] = {}
        self._lock = threading.RLock()

    def enqueue(
        self,
        *,
        job_id: str = "",
        type: str,
        tenant_id: str,
        account_id: str = "",
        space_id: str = "",
        requested_by: str = "",
        payload: dict | None = None,
        file: JobFileInput | None = None,
        max_attempts: int = 3,
        idempotency_key: str = "",
    ) -> Job:
        dedupe = (idempotency_key or "").strip()
        dedupe_scope = (tenant_id, account_id, space_id, type, dedupe)
        with self._lock:
            if dedupe:
                existing_id = self._idempotency.get(dedupe_scope)
                if existing_id and existing_id in self._jobs:
                    return self._jobs[existing_id]
        explicit_job_id = (job_id or "").strip()
        if explicit_job_id and (
            not explicit_job_id.startswith("job_") or len(explicit_job_id) > 128
        ):
            raise ValueError("Explicit job id must be an opaque job_ identifier.")
        now = _iso()
        job = Job(
            id=explicit_job_id or f"job_{uuid4().hex}",
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
            existing_by_id = self._jobs.get(job.id)
            if existing_by_id:
                if (
                    existing_by_id.type == job.type
                    and existing_by_id.tenant_id == job.tenant_id
                    and existing_by_id.account_id == job.account_id
                    and existing_by_id.space_id == job.space_id
                    and existing_by_id.payload == job.payload
                ):
                    return existing_by_id
                raise ValueError("Explicit job id already exists with different work.")
            if dedupe:
                existing_id = self._idempotency.get(dedupe_scope)
                if existing_id and existing_id in self._jobs:
                    return self._jobs[existing_id]
            self._jobs[job.id] = job
            if dedupe:
                self._idempotency[dedupe_scope] = job.id
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

    def enqueue_many(
        self,
        specs: Sequence[JobEnqueueSpec],
    ) -> tuple[Job, ...]:
        """Atomically resolve a bounded idempotent batch under one lock."""

        batch: tuple[JobEnqueueSpec, ...] = validate_job_enqueue_batch(specs)
        if not batch:
            return ()
        now = _iso()
        candidates = tuple(
            Job(
                id=(spec.job_id or "").strip() or f"job_{uuid4().hex}",
                type=spec.type,
                status=STATUS_QUEUED,
                tenant_id=spec.tenant_id,
                account_id=spec.account_id,
                space_id=spec.space_id,
                requested_by=spec.requested_by,
                payload=dict(spec.payload),
                max_attempts=int(spec.max_attempts),
                run_after=now,
                created_at=now,
                updated_at=now,
            )
            for spec in batch
        )
        with self._lock:
            staged_jobs: dict[str, Job] = {}
            staged_idempotency: dict[tuple[str, str, str, str, str], str] = {}
            resolved: list[Job] = []
            for spec, candidate in zip(batch, candidates, strict=True):
                dedupe_scope = (
                    spec.tenant_id,
                    spec.account_id,
                    spec.space_id,
                    spec.type,
                    spec.idempotency_key.strip(),
                )
                existing_id = staged_idempotency.get(dedupe_scope)
                if not existing_id:
                    existing_id = self._idempotency.get(dedupe_scope, "")
                existing = staged_jobs.get(existing_id) or self._jobs.get(existing_id)
                if existing is not None:
                    resolved.append(existing)
                    continue
                existing_by_id = staged_jobs.get(candidate.id) or self._jobs.get(candidate.id)
                if existing_by_id is not None:
                    raise ValueError(
                        "Batch job id already exists outside its idempotency key."
                    )
                staged_jobs[candidate.id] = candidate
                staged_idempotency[dedupe_scope] = candidate.id
                resolved.append(candidate)
            self._jobs.update(staged_jobs)
            self._idempotency.update(staged_idempotency)
        return tuple(resolved)

    def get(
        self,
        job_id: str,
        *,
        tenant_id: str = "",
        account_id: str = "",
        space_id: str = "",
    ) -> Job | None:
        # Memory mode has no database role boundary; keep the production store
        # signature so routers always supply the authenticated scope.
        del tenant_id, account_id, space_id
        with self._lock:
            return self._jobs.get(job_id)

    def get_file(self, job_id: str) -> JobFile | None:
        with self._lock:
            return self._files.get(job_id)

    def claim(self, worker_id: str, limit: int = 1, lease_seconds: int = 60) -> list[Job]:
        now = utcnow()
        lease_seconds = max(1, int(lease_seconds))
        lease_expires_at = _iso(now + timedelta(seconds=lease_seconds))
        out: list[Job] = []
        with self._lock:
            # A job that exhausted its final leased attempt must not become an
            # unbounded poison-message loop. Terminalizing happens under the
            # same lock as claiming so another worker cannot race the decision.
            for job in tuple(self._jobs.values()):
                if (
                    job.status == STATUS_RUNNING
                    and _lease_expired(job, now)
                    and job.attempts >= job.max_attempts
                ):
                    self._jobs[job.id] = replace(
                        job,
                        status=STATUS_FAILED,
                        result=None,
                        error=LEASE_EXPIRED_ERROR,
                        locked_by="",
                        locked_at="",
                        lease_token="",
                        lease_expires_at="",
                        updated_at=_iso(now),
                        completed_at=_iso(now),
                    )
                    self._files.pop(job.id, None)
            ready = sorted(
                (
                    job for job in self._jobs.values()
                    if (
                        job.status in READY_STATUSES and _parse(job.run_after) <= now
                    ) or (
                        job.status == STATUS_RUNNING
                        and _lease_expired(job, now)
                        and job.attempts < job.max_attempts
                    )
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
                    lease_token=f"lease_{uuid4().hex}",
                    lease_expires_at=lease_expires_at,
                    updated_at=_iso(now),
                )
                self._jobs[job.id] = updated
                out.append(updated)
        return out

    def renew_lease(self, job_id: str, lease_token: str, lease_seconds: int) -> Job:
        lease_seconds = max(1, int(lease_seconds))
        now = utcnow()
        with self._lock:
            job = self._require_active_lease(job_id, lease_token, now)
            updated = replace(
                job,
                lease_expires_at=_iso(now + timedelta(seconds=lease_seconds)),
                updated_at=_iso(now),
            )
            self._jobs[job_id] = updated
            return updated

    def mark_succeeded(self, job_id: str, result: dict, *, lease_token: str) -> Job:
        return self._update_terminal(job_id, STATUS_SUCCEEDED, lease_token=lease_token, result=result)

    def mark_failed(self, job_id: str, error: str, *, lease_token: str) -> Job:
        return self._update_terminal(job_id, STATUS_FAILED, lease_token=lease_token, error=error)

    def mark_retry(self, job_id: str, error: str, run_after: datetime, *, lease_token: str) -> Job:
        with self._lock:
            job = self._require_active_lease(job_id, lease_token, utcnow())
            updated = replace(
                job,
                status=STATUS_RETRYING,
                error=error[:2000],
                run_after=_iso(run_after),
                locked_by="",
                locked_at="",
                lease_token="",
                lease_expires_at="",
                updated_at=_iso(),
            )
            self._jobs[job_id] = updated
            return updated

    def delete_scope(
        self,
        tenant_id: str,
        *,
        account_id: str = "",
        space_id: str = "",
    ) -> JobScopeDeleteResult:
        """Delete jobs and transient bytes in one privacy scope.

        Account-wide erasure intentionally includes legacy jobs whose
        ``account_id`` predates explicit account stamping, matching the existing
        document and conversation privacy-scope behavior.
        """

        tenant_id = (tenant_id or "").strip()
        account_id = (account_id or "").strip()
        space_id = (space_id or "").strip()

        def matches(job: Job) -> bool:
            if job.tenant_id != tenant_id:
                return False
            if space_id:
                return job.account_id == account_id and job.space_id == space_id
            if account_id and job.account_id not in ("", account_id):
                return False
            return True

        with self._lock:
            job_ids = [job_id for job_id, job in self._jobs.items() if matches(job)]
            files = sum(1 for job_id in job_ids if job_id in self._files)
            for job_id in job_ids:
                self._files.pop(job_id, None)
                self._jobs.pop(job_id, None)
            removed = set(job_ids)
            self._idempotency = {
                key: job_id for key, job_id in self._idempotency.items()
                if job_id not in removed
            }
        return JobScopeDeleteResult(jobs=len(job_ids), files=files)

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

    def _update_terminal(
        self,
        job_id: str,
        status: str,
        *,
        lease_token: str,
        result: dict | None = None,
        error: str = "",
    ) -> Job:
        with self._lock:
            job = self._require_active_lease(job_id, lease_token, utcnow())
            now = _iso()
            updated = replace(
                job,
                status=status,
                result=result,
                error=error[:2000],
                locked_by="",
                locked_at="",
                lease_token="",
                lease_expires_at="",
                updated_at=now,
                completed_at=now,
            )
            self._jobs[job_id] = updated
            self._files.pop(job_id, None)
            return updated

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    def _require_active_lease(self, job_id: str, lease_token: str, now: datetime) -> Job:
        job = self._require(job_id)
        if (
            not lease_token
            or job.status != STATUS_RUNNING
            or job.lease_token != lease_token
            or _lease_expired(job, now)
        ):
            raise JobLeaseLostError(f"job lease is no longer active: {job_id}")
        return job

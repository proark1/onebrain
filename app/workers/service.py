"""Worker loop primitives."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from uuid import uuid4

from app.config import get_settings
from app.jobs.base import Job, JobStore, utcnow
from app.jobs.handlers import handle_job

LOG = logging.getLogger("onebrain.workers")


class Worker:
    def __init__(self, job_store: JobStore, worker_id: str | None = None):
        self.job_store = job_store
        self.worker_id = worker_id or f"worker_{uuid4().hex[:12]}"
        self.settings = get_settings()

    def run_once(self) -> int:
        jobs = self.job_store.claim(self.worker_id, limit=self.settings.worker_batch_size)
        for job in jobs:
            self.process(job)
        return len(jobs)

    def process(self, job: Job) -> None:
        started = time.monotonic()
        try:
            result = handle_job(job, self.job_store)
        except Exception as exc:
            self._record_failure(job, exc)
            return

        self.job_store.mark_succeeded(job.id, result)
        LOG.info(
            "job succeeded id=%s type=%s tenant=%s duration_ms=%s",
            job.id, job.type, job.tenant_id, round((time.monotonic() - started) * 1000),
        )

    def _record_failure(self, job: Job, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        retryable = not isinstance(exc, ValueError) and job.attempts < job.max_attempts
        if retryable:
            delay_seconds = min(60, 2 ** max(0, job.attempts - 1))
            self.job_store.mark_retry(job.id, message, utcnow() + timedelta(seconds=delay_seconds))
            status = "retrying"
        else:
            self.job_store.mark_failed(job.id, message)
            status = "failed"
        LOG.warning(
            "job %s id=%s type=%s tenant=%s attempts=%s/%s error=%s",
            status, job.id, job.type, job.tenant_id, job.attempts, job.max_attempts, message[:200],
        )

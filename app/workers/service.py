"""Worker loop primitives."""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import timedelta
from uuid import uuid4

from app.config import get_settings
from app.jobs.base import Job, JobLeaseLostError, JobStore, utcnow
from app.jobs.handlers import handle_job

LOG = logging.getLogger("onebrain.workers")


class Worker:
    def __init__(self, job_store: JobStore, worker_id: str | None = None):
        self.job_store = job_store
        self.worker_id = worker_id or f"worker_{uuid4().hex[:12]}"
        self.settings = get_settings()
        self._claims_stopped = threading.Event()

    def stop_claiming(self) -> None:
        """Allow the current job to settle but prevent another claim cycle."""

        self._claims_stopped.set()

    def run_once(self) -> int:
        if self._claims_stopped.is_set():
            return 0
        lease_seconds = self._lease_seconds()
        processed = 0
        # Claim one job immediately before processing it. Claiming a batch and
        # then processing sequentially leaves later jobs' leases unrenewed while
        # an earlier slow handler runs; another replica may correctly reclaim
        # them and duplicate their external work. The batch setting remains a
        # cap on work per tick, not a prefetch count.
        try:
            batch_size = max(1, int(self.settings.worker_batch_size))
        except (TypeError, ValueError):
            batch_size = 1
        for _ in range(batch_size):
            if self._claims_stopped.is_set():
                break
            jobs = self.job_store.claim(
                self.worker_id,
                limit=1,
                lease_seconds=lease_seconds,
            )
            if not jobs:
                break
            job = jobs[0]
            self.process(job, lease_seconds=lease_seconds)
            processed += 1
        return processed

    def process(self, job: Job, *, lease_seconds: int | None = None) -> None:
        if not job.lease_token:
            LOG.error("refusing unfenced job id=%s type=%s", job.id, job.type)
            return

        lease_seconds = lease_seconds or self._lease_seconds()
        started = time.monotonic()
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_lease,
            args=(job, lease_seconds, heartbeat_stop, lease_lost),
            name=f"job-lease-{job.id[:16]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = handle_job(job, self.job_store)
        except Exception as exc:
            self._stop_heartbeat(heartbeat_stop, heartbeat)
            if lease_lost.is_set():
                self._log_lease_lost(job, "failure result")
                return
            self._record_failure(job, exc)
            return

        self._stop_heartbeat(heartbeat_stop, heartbeat)
        if lease_lost.is_set():
            self._log_lease_lost(job, "success result")
            return
        try:
            self.job_store.mark_succeeded(job.id, result, lease_token=job.lease_token)
        except JobLeaseLostError:
            self._log_lease_lost(job, "success result")
            return
        except Exception:
            # Leave the fenced running row intact. A later claimant will retry
            # after expiry instead of losing a completed handler result.
            LOG.exception("job success could not be persisted id=%s type=%s", job.id, job.type)
            return
        LOG.info(
            "job succeeded id=%s type=%s tenant=%s duration_ms=%s",
            job.id, job.type, job.tenant_id, round((time.monotonic() - started) * 1000),
        )

    def _record_failure(self, job: Job, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        retryable = not isinstance(exc, ValueError) and job.attempts < job.max_attempts
        try:
            if retryable:
                delay_seconds = min(60, 2 ** max(0, job.attempts - 1))
                self.job_store.mark_retry(
                    job.id,
                    message,
                    utcnow() + timedelta(seconds=delay_seconds),
                    lease_token=job.lease_token,
                )
                status = "retrying"
            else:
                self.job_store.mark_failed(job.id, message, lease_token=job.lease_token)
                status = "failed"
        except JobLeaseLostError:
            self._log_lease_lost(job, "failure result")
            return
        except Exception:
            LOG.exception("job failure could not be persisted id=%s type=%s", job.id, job.type)
            return
        LOG.warning(
            "job %s id=%s type=%s tenant=%s attempts=%s/%s error=%s",
            status, job.id, job.type, job.tenant_id, job.attempts, job.max_attempts, message[:200],
        )

    def _lease_seconds(self) -> int:
        try:
            return max(1, int(getattr(self.settings, "job_lease_seconds", 60)))
        except (TypeError, ValueError):
            return 60

    def _heartbeat_seconds(self, lease_seconds: int) -> float:
        default = max(0.05, min(15.0, lease_seconds / 3))
        try:
            configured = float(getattr(self.settings, "job_lease_heartbeat_seconds", default))
        except (TypeError, ValueError):
            configured = default
        if configured <= 0 or not math.isfinite(configured):
            configured = default
        # A heartbeat at or after the expiry is not a heartbeat. Clamp unsafe
        # config values so a worker always renews before its lease boundary.
        return max(0.01, min(configured, max(0.05, lease_seconds / 2)))

    def _heartbeat_lease(
        self,
        job: Job,
        lease_seconds: int,
        stop: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        interval = self._heartbeat_seconds(lease_seconds)
        while not stop.wait(interval):
            try:
                self.job_store.renew_lease(job.id, job.lease_token, lease_seconds)
            except JobLeaseLostError:
                lease_lost.set()
                self._log_lease_lost(job, "heartbeat")
                return
            except Exception:
                # Do not fail the handler merely because a transient database
                # renewal failed. The next renewal or fenced terminal update
                # will determine whether the lease remains valid.
                LOG.exception("job lease heartbeat failed id=%s type=%s", job.id, job.type)

    @staticmethod
    def _stop_heartbeat(stop: threading.Event, heartbeat: threading.Thread) -> None:
        stop.set()
        heartbeat.join(timeout=1)

    def _log_lease_lost(self, job: Job, outcome: str) -> None:
        LOG.warning(
            "job lease lost; discarding %s id=%s type=%s tenant=%s",
            outcome,
            job.id,
            job.type,
            job.tenant_id,
        )

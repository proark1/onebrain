"""Thread-safe local stores for user-management jobs and encrypted receipts."""

from __future__ import annotations

import threading
from dataclasses import replace

from app.user_management.base import UserManagementJob, UserManagementReceipt


class MemoryUserManagementJobStore:
    def __init__(self):
        self._jobs: dict[str, UserManagementJob] = {}
        self._lock = threading.RLock()

    def create(self, job: UserManagementJob) -> UserManagementJob:
        with self._lock:
            if job.id in self._jobs:
                raise ValueError(f"user-management job already exists: {job.id}")
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> UserManagementJob | None:
        return self._jobs.get(job_id)

    def list_for_deployment(self, deployment_id: str, limit: int = 100) -> list[UserManagementJob]:
        rows = [job for job in self._jobs.values() if job.deployment_id == deployment_id]
        return sorted(rows, key=lambda job: (job.created_at, job.id), reverse=True)[:max(1, min(limit, 500))]

    def lease_next(self, deployment_id: str, *, now_iso: str, lease_expires_at: str) -> UserManagementJob | None:
        with self._lock:
            candidates = sorted(self._jobs.values(), key=lambda job: (job.created_at, job.id))
            for job in candidates:
                if job.deployment_id != deployment_id:
                    continue
                if job.expires_at <= now_iso:
                    if job.status in {"queued", "leased"}:
                        self._jobs[job.id] = replace(job, status="expired", error_code="command_expired")
                    continue
                if job.status == "queued" or (job.status == "leased" and job.lease_expires_at <= now_iso):
                    leased = replace(
                        job,
                        status="leased",
                        leased_at=now_iso,
                        lease_expires_at=lease_expires_at,
                        attempts=job.attempts + 1,
                    )
                    self._jobs[job.id] = leased
                    return leased
            return None

    def complete(
        self,
        job_id: str,
        deployment_id: str,
        *,
        sender_public_key: str,
        nonce: str,
        ciphertext: str,
        completed_at: str,
        result_expires_at: str,
    ) -> UserManagementJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.deployment_id != deployment_id:
                return None
            if job.status == "completed":
                return job
            if job.status != "leased":
                return None
            completed = replace(
                job,
                status="completed",
                completed_at=completed_at,
                lease_expires_at="",
                result_sender_public_key=sender_public_key,
                result_nonce=nonce,
                result_ciphertext=ciphertext,
                result_expires_at=result_expires_at,
                error_code="",
            )
            self._jobs[job_id] = completed
            return completed

    def fail(self, job_id: str, deployment_id: str, *, error_code: str, completed_at: str) -> UserManagementJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.deployment_id != deployment_id:
                return None
            failed = replace(
                job,
                status="failed",
                completed_at=completed_at,
                lease_expires_at="",
                error_code=error_code,
            )
            self._jobs[job_id] = failed
            return failed

    def consume_result(self, job_id: str, *, consumed_at: str) -> UserManagementJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "completed" or job.result_consumed_at:
                return None
            if not job.result_ciphertext or job.result_expires_at <= consumed_at:
                return None
            self._jobs[job_id] = replace(
                job,
                result_consumed_at=consumed_at,
                sealed_result_private_key="",
                result_sender_public_key="",
                result_nonce="",
                result_ciphertext="",
            )
            return job

    def expire_and_purge(self, *, now_iso: str) -> int:
        with self._lock:
            changed = 0
            for job_id, job in list(self._jobs.items()):
                if job.status in {"queued", "leased"} and job.expires_at <= now_iso:
                    self._jobs[job_id] = replace(job, status="expired", error_code="command_expired")
                    changed += 1
                elif job.result_expires_at and job.result_expires_at <= now_iso and job.result_ciphertext:
                    self._jobs[job_id] = replace(
                        job,
                        sealed_result_private_key="",
                        result_sender_public_key="",
                        result_nonce="",
                        result_ciphertext="",
                    )
                    changed += 1
            return changed


class MemoryUserManagementReceiptStore:
    def __init__(self):
        self._receipts: dict[str, UserManagementReceipt] = {}
        self._lock = threading.RLock()

    def get(self, command_id: str) -> UserManagementReceipt | None:
        return self._receipts.get(command_id)

    def put(self, receipt: UserManagementReceipt) -> UserManagementReceipt:
        with self._lock:
            existing = self._receipts.get(receipt.command_id)
            if existing:
                return existing
            self._receipts[receipt.command_id] = receipt
            return receipt

    def purge(self, *, now_iso: str) -> int:
        with self._lock:
            stale = [key for key, value in self._receipts.items() if value.expires_at <= now_iso]
            for key in stale:
                del self._receipts[key]
            return len(stale)

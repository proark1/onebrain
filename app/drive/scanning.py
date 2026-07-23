"""Fenced Drive malware-scan orchestration over replaceable scanner adapters."""

from __future__ import annotations

import uuid
import time
import threading
from datetime import datetime, timedelta, timezone

from app.drive.base import (
    DriveConflictError,
    DriveMalwareCompletion,
    DriveMalwareWorkerStore,
    ScannerRuntimeStatus,
    drive_ingest_idempotency_key,
    drive_ingest_job_id,
    now_iso,
)
from app.drive.malware.base import (
    MalwareScanError,
    ScannerReadiness,
    ScanRequest,
    ScanVerdict,
)
from app.accounting.base import accounting_category_id
from app.drive.service import DriveService
from app.jobs.base import (
    JOB_ACCOUNTING_EXTRACT,
    JOB_DRIVE_FILE_INGEST,
    JOB_DRIVE_REVISION_MALWARE_SCAN,
)
from app.platform.base import AuditEvent


class DriveMalwareScanningService:
    """Scan immutable revisions and commit evidence only through a live job fence."""

    def __init__(
        self,
        *,
        store: DriveMalwareWorkerStore,
        blobs,
        scanner,
        job_store,
        settings,
        platform_store=None,
        worker_id: str = "worker_unknown",
    ):
        self.store: DriveMalwareWorkerStore = store
        self.blobs = blobs
        self.scanner = scanner
        self.job_store = job_store
        self.settings = settings
        self.platform_store = platform_store
        self.worker_id = worker_id
        self._last_successful_refresh_at = ""
        self._next_heartbeat_at = 0.0
        self._tenant_cursor = ""
        self._last_readiness = ""
        self._retry_wakeup_pending = False
        self._readiness_lock = threading.Lock()
        self._upload_maintenance = DriveService(
            store=store,
            blobs=blobs,
            platform_store=platform_store,
            job_store=job_store,
            settings=settings,
        )

    def handle(self, job) -> dict:
        scan_id = str(job.payload.get("scan_id") or "")
        revision_id = str(job.payload.get("revision_id") or "")
        if not scan_id or not revision_id or not job.lease_token:
            raise ValueError("Drive malware job is missing its fenced attempt identity.")
        scan = self.store.get_malware_scan(
            scan_id, account_id=job.account_id, space_id=job.space_id,
        )
        if not scan or scan.revision_id != revision_id:
            return {"status": "stale", "scan_id": scan_id}
        if scan.status in {"clean", "infected", "scan_error"}:
            self._record_terminal_audit(scan, worker_id=job.locked_by)
            return {
                "status": scan.status,
                "scan_id": scan.id,
                "revision_id": scan.revision_id,
                "file_id": scan.file_id,
                "ingestion_queued": False,
                "idempotent_replay": True,
            }

        attempt_fence = f"fence_{uuid.uuid4().hex}"
        try:
            active = self.store.begin_malware_scan(
                job_id=job.id,
                lease_token=job.lease_token,
                lease_expires_at=job.lease_expires_at,
                scan_id=scan.id,
                attempt_fence=attempt_fence,
            )
        except DriveConflictError:
            return {"status": "stale", "scan_id": scan_id}

        revision = self.store.get_revision(
            active.revision_id, account_id=active.account_id, space_id=active.space_id,
        )
        verdict = self._scan_revision(active, revision)
        failures = active.consecutive_failures + 1 if verdict.status == "scan_error" else 0
        next_attempt_at = self._next_attempt_at(failures) if verdict.status == "scan_error" else ""
        completion = self.store.complete_malware_scan(
            job_id=job.id,
            lease_token=job.lease_token,
            scan_id=active.id,
            attempt_fence=attempt_fence,
            verdict=verdict,
            next_attempt_at=next_attempt_at,
            consecutive_failures=failures,
        )
        self._enqueue_ingestion_if_needed(completion)
        self._enqueue_accounting_extraction_if_needed(completion)
        self._record_terminal_audit(completion.scan, worker_id=job.locked_by)
        self._publish_runtime_status(job.tenant_id, verdict)
        return {
            "status": completion.scan.status,
            "scan_id": completion.scan.id,
            "revision_id": completion.scan.revision_id,
            "file_id": completion.scan.file_id,
            "ingestion_queued": bool(completion.ingestion_job_id),
        }

    def reconcile(self, *, limit: int = 100) -> dict:
        woken = self._continue_retry_wakeup(limit=limit)
        result = self.store.reconcile_malware_scans(limit=limit)
        drained = self._drain_scan_outbox(limit=limit)
        expired_uploads = self._upload_maintenance.cleanup_expired_uploads_for_deployment(
            worker_store=self.store,
            limit=limit,
        )
        return {
            "woken_attempts": woken,
            "recovered_attempts": result.recovered_attempts,
            "created_attempts": result.created_attempts,
            "enqueued_jobs": result.enqueued_jobs,
            "drained_jobs": drained,
            "expired_uploads": expired_uploads,
        }

    def refresh_definitions_if_due(self) -> bool:
        refresh = getattr(self.scanner, "refresh_definitions_if_due", None)
        if not callable(refresh):
            return False
        refreshed = bool(refresh())
        if refreshed:
            self._last_successful_refresh_at = now_iso()
            self._wake_retryable_attempts(limit=100)
        return refreshed

    def heartbeat_if_due(self, *, force: bool = False) -> int:
        monotonic_now = time.monotonic()
        if not force and monotonic_now < self._next_heartbeat_at:
            return 0
        interval = max(
            10.0,
            float(getattr(self.settings, "drive_malware_runtime_stale_seconds", 180)) / 3,
        )
        self._next_heartbeat_at = monotonic_now + interval
        tenant_ids = self._next_tenant_page()
        readiness = self._safe_readiness()
        self._observe_readiness(readiness)
        for tenant_id in tenant_ids:
            self._upsert_tenant_status(tenant_id, readiness=readiness)
        return len(tenant_ids)

    def _scan_revision(self, scan, revision) -> ScanVerdict:
        if not revision or revision.file_id != scan.file_id:
            return self._error_verdict(scan, "revision_missing")
        try:
            info = self.blobs.stat(revision.storage_key)
            if (
                not info
                or info.sha256 != revision.sha256
                or info.size_bytes != revision.size_bytes
            ):
                return self._error_verdict(scan, "blob_integrity_mismatch")
            request = ScanRequest(
                revision_id=revision.id,
                expected_sha256=revision.sha256,
                expected_size_bytes=revision.size_bytes,
                policy_epoch=scan.policy_epoch,
                content=self.blobs.iter_range(revision.storage_key),
            )
            return self.scanner.scan(request)
        except MalwareScanError as exc:
            return self._error_verdict(scan, exc.code)
        except Exception:
            # Scanner implementation defects remain content-free and fail
            # closed. The attempt is retryable through the normal cooldown
            # lifecycle; no raw exception or scanner output is persisted.
            return self._error_verdict(scan, "scanner_unhandled_error")

    def _error_verdict(self, scan, code: str) -> ScanVerdict:
        readiness = self._safe_readiness()
        self._observe_readiness(readiness)
        return ScanVerdict(
            status="scan_error",
            sha256=scan.revision_sha256,
            size_bytes=scan.revision_size_bytes,
            engine=readiness.engine or "onebrain-gate",
            engine_version=readiness.engine_version or "1",
            definition_version=readiness.definition_version or "unavailable",
            definition_timestamp=readiness.definition_timestamp or now_iso(),
            error_code=code,
        )

    def _next_attempt_at(self, failures: int) -> str:
        immediate = max(1, int(getattr(self.settings, "drive_malware_retry_attempts", 5)))
        if failures < immediate:
            return now_iso()
        base = max(1, int(getattr(
            self.settings, "drive_malware_retry_cooldown_seconds", 15 * 60,
        )))
        maximum = max(base, int(getattr(
            self.settings, "drive_malware_retry_max_cooldown_seconds", 6 * 60 * 60,
        )))
        cooldown_cycle = max(0, failures - immediate)
        delay = min(maximum, base * (2 ** min(cooldown_cycle, 20)))
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    def _enqueue_ingestion_if_needed(self, completion: DriveMalwareCompletion) -> None:
        file = completion.file
        if not completion.ingestion_job_id or not file:
            return
        expected_job_id = drive_ingest_job_id(
            file.id,
            file.current_revision_id,
            file.generation,
        )
        if completion.ingestion_job_id != expected_job_id:
            raise RuntimeError("Drive ingestion job identity did not match scan completion.")
        job = self.job_store.enqueue(
            job_id=completion.ingestion_job_id,
            type=JOB_DRIVE_FILE_INGEST,
            tenant_id=file.tenant_id,
            account_id=file.account_id,
            space_id=file.space_id,
            requested_by=file.uploaded_by,
            payload={
                "file_id": file.id,
                "revision_id": file.current_revision_id,
                "generation": file.generation,
            },
            max_attempts=max(1, int(getattr(self.settings, "job_max_attempts", 3))),
            idempotency_key=drive_ingest_idempotency_key(
                file.id,
                file.current_revision_id,
                file.generation,
            ),
        )
        if job.id != completion.ingestion_job_id:
            raise RuntimeError("Drive ingestion job identity did not match scan completion.")

    def _enqueue_accounting_extraction_if_needed(self, completion: DriveMalwareCompletion) -> None:
        """Kick invoice extraction when a clean file is in the buchhaltung category.

        Deliberately independent of ``ingestion_job_id`` / ``desired_indexed``: an
        accounting file the user did NOT mark "index for AI" produces no Drive
        ingest job, but it must still be extracted. The only signals are a clean
        verdict and the accounting AccessGroup category (derived per space so it
        matches what the install bootstrap seeded and the upload picker sets).
        Idempotent on (file, revision, generation) so a re-scan can't double-book.
        """
        file = completion.file
        if not file or completion.scan.status != "clean":
            return
        if file.category != accounting_category_id(file.space_id):
            return
        self.job_store.enqueue(
            type=JOB_ACCOUNTING_EXTRACT,
            tenant_id=file.tenant_id,
            account_id=file.account_id,
            space_id=file.space_id,
            requested_by=file.uploaded_by,
            payload={
                "file_id": file.id,
                "revision_id": file.current_revision_id,
                "generation": file.generation,
            },
            max_attempts=max(1, int(getattr(self.settings, "job_max_attempts", 3))),
            idempotency_key=(
                f"accounting-extract:{file.id}:{file.current_revision_id}:{file.generation}"
            ),
        )

    def _drain_scan_outbox(self, *, limit: int) -> int:
        drained = 0
        for spec in self.store.list_pending_malware_job_specs(limit=limit):
            job = self.job_store.enqueue(
                job_id=spec.job_id,
                type=JOB_DRIVE_REVISION_MALWARE_SCAN,
                tenant_id=spec.tenant_id,
                account_id=spec.account_id,
                space_id=spec.space_id,
                requested_by=spec.requested_by,
                payload={
                    "scan_id": spec.scan_id,
                    "revision_id": spec.revision_id,
                    "origin": spec.origin,
                },
                max_attempts=spec.max_attempts,
                idempotency_key=spec.idempotency_key,
            )
            if job.id != spec.job_id:
                raise RuntimeError("Malware scan queue identity did not match its outbox.")
            self.store.acknowledge_malware_job_spec(spec.job_id)
            drained += 1
        return drained

    def _publish_runtime_status(
        self, tenant_id: str, verdict: ScanVerdict, *, worker_id: str = "",
    ) -> None:
        readiness = self._safe_readiness()
        self._observe_readiness(readiness)
        self._upsert_tenant_status(
            tenant_id,
            readiness=readiness,
            verdict=verdict,
            worker_id=worker_id,
        )

    def _upsert_tenant_status(
        self,
        tenant_id: str,
        *,
        readiness: ScannerReadiness,
        verdict: ScanVerdict | None = None,
        worker_id: str = "",
    ) -> None:
        heartbeat = now_iso()
        resolved_worker_id = worker_id or self.worker_id
        existing = next((
            row for row in self.store.list_scanner_runtime_status(tenant_id=tenant_id)
            if row.worker_id == resolved_worker_id
        ), None)
        errors = dict(existing.recent_error_counts) if existing else {}
        if verdict and verdict.status == "scan_error":
            errors[verdict.error_code] = min(1_000_000, int(errors.get(verdict.error_code, 0)) + 1)
        errors = dict(sorted(errors.items())[:20])
        counts = self.store.malware_operational_counts(tenant_id=tenant_id)
        status = ScannerRuntimeStatus(
            tenant_id=tenant_id,
            worker_id=resolved_worker_id,
            readiness=readiness.readiness,
            scanner_engine=readiness.engine,
            scanner_engine_version=readiness.engine_version,
            definition_version=readiness.definition_version,
            definition_timestamp=readiness.definition_timestamp,
            last_successful_refresh_at=(
                self._last_successful_refresh_at
                or (existing.last_successful_refresh_at if existing else "")
            ),
            last_successful_scan_at=(
                heartbeat if verdict and verdict.status in {"clean", "infected"}
                else (existing.last_successful_scan_at if existing else "")
            ),
            pending_count=counts.pending_count,
            recent_error_counts=errors,
            heartbeat_at=heartbeat,
        )
        self.store.upsert_scanner_runtime_status(status)

    def _observe_readiness(self, readiness: ScannerReadiness) -> None:
        with self._readiness_lock:
            previous = self._last_readiness
            self._last_readiness = readiness.readiness
        if readiness.readiness == "ready" and previous in {"degraded", "unknown"}:
            self._wake_retryable_attempts(limit=100)

    def _wake_retryable_attempts(self, *, limit: int) -> int:
        bounded = max(1, min(int(limit), 1_000))
        woken = self.store.wake_retryable_malware_scans(limit=bounded)
        self._retry_wakeup_pending = woken >= bounded
        return woken

    def _continue_retry_wakeup(self, *, limit: int) -> int:
        if not self._retry_wakeup_pending:
            return 0
        return self._wake_retryable_attempts(limit=limit)

    def _next_tenant_page(self) -> list[str]:
        lister = getattr(self.store, "list_malware_tenant_ids", None)
        if callable(lister):
            tenant_ids = list(lister(after=self._tenant_cursor, limit=1_000))
            if not tenant_ids and self._tenant_cursor:
                self._tenant_cursor = ""
                tenant_ids = list(lister(after="", limit=1_000))
        elif self.platform_store is None:
            tenant_ids = []
        else:
            tenant_ids = sorted({
                account.id for account in self.platform_store.list_accounts()
                if getattr(account, "status", "active") == "active"
                and account.id > self._tenant_cursor
            })[:1_000]
            if not tenant_ids and self._tenant_cursor:
                self._tenant_cursor = ""
                tenant_ids = sorted({
                    account.id for account in self.platform_store.list_accounts()
                    if getattr(account, "status", "active") == "active"
                })[:1_000]
        if tenant_ids:
            self._tenant_cursor = tenant_ids[-1]
        return tenant_ids

    def _safe_readiness(self) -> ScannerReadiness:
        try:
            return self.scanner.readiness()
        except Exception:
            return ScannerReadiness(
                readiness="unknown",
                engine="onebrain-gate",
                engine_version="1",
                definition_version="unavailable",
                definition_timestamp=now_iso(),
                reason_code="readiness_error",
            )

    def _record_terminal_audit(self, scan, *, worker_id: str = "") -> None:
        if self.platform_store is None or scan.status not in {"clean", "infected", "scan_error"}:
            return
        audit_id = f"audit_{uuid.uuid5(uuid.NAMESPACE_URL, f'onebrain:drive-malware:{scan.id}').hex}"
        existing = self.platform_store.list_audit(scan.account_id)
        if any(event.id == audit_id for event in existing):
            return
        event = AuditEvent(
            id=audit_id,
            account_id=scan.account_id,
            space_id=scan.space_id,
            actor_id=worker_id or self.worker_id,
            actor_type="system",
            action="drive.revision.malware_scanned",
            target_type="drive_revision",
            target_id=scan.revision_id,
            app_id="onebrain_core",
            purpose="knowledge_management",
            decision=scan.status,
            meta={
                "scan_id": scan.id,
                "file_id": scan.file_id,
                "status": scan.status,
                "policy_epoch": scan.policy_epoch,
                "scanner_engine": scan.scanner_engine,
                "scanner_engine_version": scan.scanner_engine_version,
                "definition_version": scan.definition_version,
                "threat_code": scan.threat_code,
                "error_code": scan.error_code,
            },
        )
        try:
            self.platform_store.record_audit(event)
        except Exception:
            # A retry may race after the first writer committed. Suppress only
            # a verified duplicate; every other audit failure remains visible
            # to the job retry path.
            if any(row.id == audit_id for row in self.platform_store.list_audit(scan.account_id)):
                return
            raise


def mark_drive_malware_job_failed(job) -> None:
    """Do not invent a verdict after an unfenced handler failure.

    Reconciliation observes the expired scan/job lease and creates the next
    append-only attempt. This hook intentionally performs no ordinary update.
    """

    del job

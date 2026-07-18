from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.auth.principal import Principal
from app.drive.blobs import LocalDriveBlobStore
from app.drive.base import now_iso
from app.drive.malware.fake import FakeMalwareScanner
from app.drive.memory import MemoryDriveStore
from app.drive.scanning import DriveMalwareScanningService
from app.drive.service import DriveService
from app.jobs.base import (
    JOB_DRIVE_FILE_INGEST,
    JOB_DRIVE_REVISION_MALWARE_SCAN,
)
from app.jobs.memory import MemoryJobStore
from app.platform.base import Account, Space
from app.platform.memory import MemoryPlatformStore
from app.security.policy import Classification
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"
OWNER = "user_owner"


def _fixture(tmp_path, *, outcomes=("clean",), index_for_ai=True):
    vectors = MemoryStore()
    drive = MemoryDriveStore(vectors)
    blobs = LocalDriveBlobStore(
        str(tmp_path / "drive"), min_free_bytes=0, min_free_percent=0,
    )
    jobs = MemoryJobStore()
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=ACCOUNT, kind="organization", name="Acme", owner_user_id=OWNER,
    ))
    platform.create_space(Space(
        id=SPACE, account_id=ACCOUNT, kind="business", name="Company",
    ))
    settings = SimpleNamespace(
        drive_max_file_bytes=1024,
        drive_upload_session_seconds=3600,
        drive_policy_mode="storage_and_indexing",
        drive_private_spaces_enabled=False,
        drive_malware_quarantine_bytes=1024 * 1024,
        drive_malware_retry_attempts=5,
        drive_malware_retry_cooldown_seconds=900,
        drive_malware_retry_max_cooldown_seconds=21_600,
        job_max_attempts=3,
    )
    service = DriveService(
        store=drive,
        blobs=blobs,
        platform_store=platform,
        job_store=jobs,
        settings=settings,
    )
    principal = Principal(
        user_id=OWNER,
        role_id="admin",
        role_label="Admin",
        clearance=Classification.RESTRICTED,
        locations=None,
        categories=None,
        location_label="all locations",
        tenant_id=ACCOUNT,
    )
    payload = b"OneBrain security handbook"
    upload = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        size_bytes=len(payload),
        index_for_ai=index_for_ai,
        idempotency_key="scan-fixture",
    )
    started, writer = service.begin_upload(principal, upload.id)
    writer.write(payload)
    uploaded = service.finish_upload_content(
        principal, started, writer.finish(), "text/plain",
    )
    _completed, file = service.complete_upload(principal, uploaded.id)
    scanning = DriveMalwareScanningService(
        store=drive,
        blobs=blobs,
        scanner=FakeMalwareScanner(outcomes),
        job_store=jobs,
        platform_store=platform,
        settings=settings,
        worker_id="worker_test",
    )
    return service, scanning, jobs, vectors, principal, file, payload


def _claim_scan(jobs):
    claimed = jobs.claim("worker_test", limit=1, lease_seconds=60)
    assert len(claimed) == 1
    assert claimed[0].type == JOB_DRIVE_REVISION_MALWARE_SCAN
    return claimed[0]


def test_clean_scan_is_the_only_transition_that_queues_ingestion(tmp_path):
    service, scanning, jobs, vectors, _principal, file, _payload = _fixture(tmp_path)

    result = scanning.handle(_claim_scan(jobs))

    assert result["status"] == "clean"
    assert result["ingestion_queued"] is True
    stored = service.store.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)
    assert stored.index_status == "queued"
    assert service.malware_status(stored) == "clean"
    assert service.store.quarantine_usage_bytes() == 0
    assert vectors.count() == 0
    assert [job.type for job in jobs._jobs.values()].count(JOB_DRIVE_FILE_INGEST) == 1
    audits = service.platform_store.list_audit(ACCOUNT)
    scan_audit = next(row for row in audits if row.action == "drive.revision.malware_scanned")
    assert scan_audit.decision == "clean"
    assert scan_audit.target_id == file.current_revision_id
    assert "handbook.txt" not in repr(scan_audit.meta)


def test_infected_scan_stays_quarantined_and_never_queues_ai_work(tmp_path):
    service, scanning, jobs, vectors, principal, file, payload = _fixture(
        tmp_path, outcomes=("infected",),
    )

    result = scanning.handle(_claim_scan(jobs))

    assert result["status"] == "infected"
    assert result["ingestion_queued"] is False
    stored = service.store.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)
    assert stored.index_status == "blocked"
    assert service.malware_status(stored) == "infected"
    assert service.store.quarantine_usage_bytes() == len(payload)
    assert vectors.count() == 0
    assert not any(job.type == JOB_DRIVE_FILE_INGEST for job in jobs._jobs.values())
    with pytest.raises(PermissionError, match="drive_revision_quarantined"):
        service.get_revision_for_download(
            principal, account_id=ACCOUNT, space_id=SPACE, file_id=file.id,
        )


def test_scanner_outage_persists_bounded_error_and_reconciles_a_new_attempt(tmp_path):
    service, scanning, jobs, vectors, _principal, file, _payload = _fixture(
        tmp_path, outcomes=("unavailable", "clean"),
    )
    scan_job = _claim_scan(jobs)

    result = scanning.handle(scan_job)
    jobs.mark_succeeded(scan_job.id, result, lease_token=scan_job.lease_token)

    assert result["status"] == "scan_error"
    failed = service.store.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert failed.error_code == "scanner_unavailable"
    assert failed.consecutive_failures == 1
    assert failed.next_attempt_at
    assert vectors.count() == 0

    repaired = scanning.reconcile(limit=10)
    current = service.store.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert repaired["created_attempts"] == 1
    assert repaired["drained_jobs"] == 1
    assert current.attempt_sequence == failed.attempt_sequence + 1
    assert current.status == "pending"
    assert current.consecutive_failures == 1
    assert any(
        job.type == JOB_DRIVE_REVISION_MALWARE_SCAN and job.id == current.job_id
        for job in jobs._jobs.values()
    )


def test_successful_definition_refresh_wakes_cooldown_attempt_immediately(tmp_path):
    service, scanning, jobs, _vectors, _principal, file, _payload = _fixture(
        tmp_path, outcomes=("unavailable",),
    )
    scanning.settings.drive_malware_retry_attempts = 1
    scan_job = _claim_scan(jobs)
    result = scanning.handle(scan_job)
    jobs.mark_succeeded(scan_job.id, result, lease_token=scan_job.lease_token)
    failed = service.store.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert failed.next_attempt_at > now_iso()

    class RefreshingScanner(FakeMalwareScanner):
        def refresh_definitions_if_due(self):
            return True

    scanning.scanner = RefreshingScanner(("clean",))
    assert scanning.refresh_definitions_if_due() is True
    eligible = service.store.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert eligible.next_attempt_at <= now_iso()

    repaired = scanning.reconcile(limit=10)
    assert repaired["created_attempts"] == 1


def test_degraded_to_ready_heartbeat_wakes_cooldown_attempt(tmp_path):
    service, scanning, jobs, _vectors, _principal, file, _payload = _fixture(
        tmp_path, outcomes=("unavailable",),
    )
    scanning.settings.drive_malware_retry_attempts = 1
    scan_job = _claim_scan(jobs)
    result = scanning.handle(scan_job)
    jobs.mark_succeeded(scan_job.id, result, lease_token=scan_job.lease_token)

    scanning.scanner = FakeMalwareScanner(("clean",))
    scanning.heartbeat_if_due(force=True)
    eligible = service.store.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert eligible.next_attempt_at <= now_iso()


def test_terminal_job_replay_keeps_scan_audit_idempotent(tmp_path):
    service, scanning, jobs, _vectors, _principal, _file, _payload = _fixture(tmp_path)
    scan_job = _claim_scan(jobs)

    first = scanning.handle(scan_job)
    replay = scanning.handle(scan_job)

    assert first["status"] == replay["status"] == "clean"
    assert replay["idempotent_replay"] is True
    assert len([
        row for row in service.platform_store.list_audit(ACCOUNT)
        if row.action == "drive.revision.malware_scanned"
    ]) == 1


def test_readiness_failure_is_fail_closed_without_losing_scan_evidence(tmp_path):
    service, scanning, jobs, _vectors, _principal, file, _payload = _fixture(tmp_path)

    class BrokenReadinessScanner(FakeMalwareScanner):
        def readiness(self):
            raise RuntimeError("raw scanner diagnostic")

    scanning.scanner = BrokenReadinessScanner(("clean",))
    result = scanning.handle(_claim_scan(jobs))

    assert result["status"] == "clean"
    runtime = service.store.list_scanner_runtime_status(tenant_id=ACCOUNT)
    assert runtime[0].readiness == "unknown"
    assert runtime[0].recent_error_counts == {}
    assert "raw scanner diagnostic" not in repr(runtime[0])


def test_blob_backend_failure_persists_sanitized_scan_error(tmp_path):
    service, scanning, jobs, _vectors, _principal, file, _payload = _fixture(tmp_path)

    class BrokenBlobStore:
        def stat(self, _storage_key):
            raise OSError("C:/secret/raw-path")

    scanning.blobs = BrokenBlobStore()
    result = scanning.handle(_claim_scan(jobs))
    stored = service.store.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )

    assert result["status"] == "scan_error"
    assert stored.error_code == "scanner_unhandled_error"
    assert "secret" not in repr(stored)


def test_idle_worker_heartbeat_reports_ready_with_real_zero_counts(tmp_path):
    vectors = MemoryStore()
    drive = MemoryDriveStore(vectors)
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=ACCOUNT, kind="organization", name="Acme", owner_user_id=OWNER,
    ))
    settings = SimpleNamespace(
        drive_malware_runtime_stale_seconds=180,
        drive_malware_retry_attempts=5,
        drive_malware_retry_cooldown_seconds=900,
        drive_malware_retry_max_cooldown_seconds=21_600,
        job_max_attempts=3,
    )
    scanning = DriveMalwareScanningService(
        store=drive,
        blobs=LocalDriveBlobStore(
            str(tmp_path / "idle-drive"), min_free_bytes=0, min_free_percent=0,
        ),
        scanner=FakeMalwareScanner(("clean",)),
        job_store=MemoryJobStore(),
        platform_store=platform,
        settings=settings,
        worker_id="worker_idle",
    )

    assert scanning.heartbeat_if_due(force=True) == 1
    status = drive.list_scanner_runtime_status(tenant_id=ACCOUNT)[0]
    counts = drive.malware_operational_counts(tenant_id=ACCOUNT)

    assert status.readiness == "ready"
    assert status.pending_count == 0
    assert status.last_successful_scan_at == ""
    assert counts.pending_count == 0
    assert counts.quarantine_usage_bytes == 0


def test_idle_worker_heartbeat_is_a_safe_noop_without_a_platform_store(tmp_path):
    scanning = DriveMalwareScanningService(
        store=MemoryDriveStore(MemoryStore()),
        blobs=LocalDriveBlobStore(
            str(tmp_path / "no-platform-drive"), min_free_bytes=0, min_free_percent=0,
        ),
        scanner=FakeMalwareScanner(("clean",)),
        job_store=MemoryJobStore(),
        platform_store=None,
        settings=SimpleNamespace(drive_malware_runtime_stale_seconds=180),
        worker_id="worker_no_platform",
    )

    assert scanning.heartbeat_if_due(force=True) == 0


def test_successful_definition_refresh_is_retained_by_periodic_heartbeat(tmp_path):
    service, scanning, _jobs, _vectors, _principal, _file, _payload = _fixture(tmp_path)

    class RefreshingScanner(FakeMalwareScanner):
        def refresh_definitions_if_due(self):
            return True

    scanning.scanner = RefreshingScanner(("clean",))
    assert scanning.refresh_definitions_if_due() is True
    scanning.heartbeat_if_due(force=True)

    status = service.store.list_scanner_runtime_status(tenant_id=ACCOUNT)[0]
    assert status.last_successful_refresh_at
    assert status.pending_count == 1


def test_periodic_reconcile_cleans_expired_reservations_across_accounts(tmp_path):
    service, scanning, _jobs, _vectors, _principal, _file, _payload = _fixture(tmp_path)
    other_account = "tenant_other"
    other_space = "space_other"
    service.platform_store.create_account(Account(
        id=other_account, kind="organization", name="Other", owner_user_id="user_other",
    ))
    service.platform_store.create_space(Space(
        id=other_space, account_id=other_account, kind="business", name="Other Company",
    ))
    other = Principal(
        user_id="user_other",
        role_id="admin",
        role_label="Admin",
        clearance=Classification.RESTRICTED,
        locations=None,
        categories=None,
        location_label="all locations",
        tenant_id=other_account,
    )
    upload = service.create_upload(
        other,
        account_id=other_account,
        space_id=other_space,
        folder_id="",
        name="abandoned.txt",
        size_bytes=7,
        index_for_ai=False,
        idempotency_key="other-abandoned",
    )
    uploading, writer = service.begin_upload(other, upload.id)
    writer.write(b"expired")
    writer.finish()
    service.store.update_upload(replace(
        uploading, expires_at="2020-01-01T00:00:00+00:00",
    ))

    result = scanning.reconcile(limit=10)

    expired = service.store.get_upload(upload.id, tenant_id=other_account)
    assert result["expired_uploads"] == 1
    assert expired.status == "expired"
    assert expired.reservation_state == "released"
    assert service.blobs.staging_info(upload.id) is None

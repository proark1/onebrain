from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth.principal import Principal
from app.drive.base import (
    DriveEntryPage,
    DriveFile,
    DriveFileListDetail,
    DriveMalwareScan,
    DriveMalwareOperationalCounts,
    DriveRevision,
    DriveUploadSession,
    ScannerRuntimeStatus,
    now_iso,
)
from app.drive.service import DriveService
from app.platform.base import Account
from app.platform.memory import MemoryPlatformStore
from app.routers import drive as drive_router
from app.security.policy import Classification


ACCOUNT = "tenant_account"
SPACE = "space_shared"


def _principal(*, principal_type: str = "human") -> Principal:
    return Principal(
        user_id="user_owner",
        role_id="admin" if principal_type == "human" else "service",
        role_label="Admin",
        clearance=Classification.RESTRICTED,
        locations=None,
        categories=None,
        location_label="all locations",
        tenant_id=ACCOUNT,
        principal_type=principal_type,
    )


def test_drive_request_models_reject_unknown_or_malformed_fields():
    valid = {
        "account_id": ACCOUNT,
        "space_id": SPACE,
        "name": "handbook.txt",
        "size_bytes": 10,
        "idempotency_key": "request-1",
    }
    assert drive_router.UploadCreateIn(**valid).size_bytes == 10
    with pytest.raises(ValidationError):
        drive_router.UploadCreateIn(**valid, caller_supplied_owner="attacker")
    with pytest.raises(ValidationError):
        drive_router.UploadCreateIn(**{**valid, "size_bytes": 0})


def test_drive_requires_a_human_employee_not_a_service_key():
    assert drive_router._human(_principal()).is_employee
    with pytest.raises(HTTPException) as error:
        drive_router._human(_principal(principal_type="service"))
    assert error.value.status_code == 403


def test_api_serializers_do_not_expose_blob_or_private_owner_keys():
    file = DriveFile(
        id="file_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        owner_user_id="user_owner",
        current_revision_id="revision_aaaaaaaa",
        uploaded_by="user_owner",
    )
    revision = DriveRevision(
        id="revision_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        upload_session_id="upload_aaaaaaaa",
        storage_key="drive/private/storage/path",
        sha256="a" * 64,
        size_bytes=42,
        media_type="text/plain",
        original_name=file.name,
        created_by="user_owner",
    )
    evidence = DriveMalwareScan(
        id="scan_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        revision_id=revision.id,
        revision_sha256=revision.sha256,
        revision_size_bytes=revision.size_bytes,
        status="clean",
        origin="upload",
        scanner_engine="clamav",
        scanner_engine_version="1.4.3",
        definition_version="daily-42",
        definition_timestamp="2026-07-18T00:00:00+00:00",
        completed_at="2026-07-18T00:00:01+00:00",
    )
    detail = DriveFileListDetail(revision=revision, malware_scan=evidence)
    output = drive_router._file_out(file, detail)
    assert output["size_bytes"] == 42
    assert output["download_url"].startswith("/api/drive/files/")
    assert output["malware_scanned_at"] == evidence.completed_at
    assert output["malware_definition_version"] == "daily-42"
    assert "storage_key" not in output
    assert "owner_user_id" not in output
    assert "tenant_id" not in output

    pending = DriveMalwareScan(
        id="scan_bbbbbbbb",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        revision_id=revision.id,
        revision_sha256=revision.sha256,
        revision_size_bytes=revision.size_bytes,
        status="pending",
        origin="rescan",
    )
    quarantined = drive_router._file_out(
        file,
        DriveFileListDetail(revision=revision, malware_scan=pending),
    )
    assert quarantined["malware_status"] == "pending"
    assert quarantined["download_url"] is None

    upload = DriveUploadSession(
        id="upload_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        size_bytes=42,
        desired_indexed=True,
        classification="internal",
        location="global",
        category="general",
        created_by="user_owner",
        idempotency_key="request-1",
        staging_key="staging/private-key",
    )
    assert "staging_key" not in drive_router._upload_out(upload)


def test_download_supports_bounded_byte_ranges_and_security_headers(monkeypatch):
    payload = b"0123456789"
    digest = hashlib.sha256(payload).hexdigest()
    file = SimpleNamespace(name="policy final.txt")
    revision = SimpleNamespace(storage_key="opaque", sha256=digest, size_bytes=len(payload))
    info = SimpleNamespace(size_bytes=len(payload), sha256=digest)

    class Blobs:
        @staticmethod
        def iter_range(_key, *, start, end):
            yield payload[start:end + 1]

    service = SimpleNamespace(
        blobs=Blobs(),
        get_revision_for_download=lambda *args, **kwargs: (file, revision, info),
    )
    monkeypatch.setattr(drive_router, "get_drive_service", lambda: service)

    response = drive_router.download_file(
        "file_aaaaaaaa",
        ACCOUNT,
        SPACE,
        range_header="bytes=2-5",
        principal=_principal(),
    )

    async def body():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert asyncio.run(body()) == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["etag"] == f'"{digest}"'

    with pytest.raises(HTTPException) as error:
        drive_router.download_file(
            "file_aaaaaaaa",
            ACCOUNT,
            SPACE,
            range_header="bytes=99-100",
            principal=_principal(),
        )
    assert error.value.status_code == 416
    assert error.value.headers == {"Content-Range": "bytes */10"}


@pytest.mark.parametrize(
    "stored_size,stored_sha256",
    [
        (11, "a" * 64),
        (10, "b" * 64),
    ],
)
def test_download_fails_before_streaming_when_original_integrity_mismatches(
    monkeypatch, stored_size, stored_sha256,
):
    expected_sha256 = "a" * 64
    file = SimpleNamespace(id="file_aaaaaaaa", name="policy.txt")
    revision = SimpleNamespace(
        id="revision_aaaaaaaa",
        storage_key="opaque",
        size_bytes=10,
        sha256=expected_sha256,
    )

    class Blobs:
        iterated = False

        @staticmethod
        def stat(_key):
            return SimpleNamespace(size_bytes=stored_size, sha256=stored_sha256)

        @classmethod
        def iter_range(cls, _key, *, start, end):
            cls.iterated = True
            yield b"untrusted"

    class Platform:
        recorded = False

        @classmethod
        def record_data_access(cls, _event):
            cls.recorded = True

    class Service:
        blobs = Blobs()
        platform_store = Platform()
        get_revision_for_download = DriveService.get_revision_for_download
        require_revision_blob_integrity = DriveService.require_revision_blob_integrity

        @staticmethod
        def get_file(*args, **kwargs):
            return file

        @staticmethod
        def require_clean_current_revision(_file):
            return revision

    monkeypatch.setattr(drive_router, "get_drive_service", Service)

    with pytest.raises(HTTPException) as error:
        drive_router.download_file(
            file.id,
            ACCOUNT,
            SPACE,
            principal=_principal(),
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Drive original failed integrity validation."
    assert Blobs.iterated is False
    assert Platform.recorded is False


def test_authorized_quarantine_lock_is_a_stable_423(monkeypatch):
    from app.drive.base import DriveQuarantineLockedError

    service = SimpleNamespace(
        get_revision_for_download=lambda *args, **kwargs: (_ for _ in ()).throw(
            DriveQuarantineLockedError()
        ),
    )
    monkeypatch.setattr(drive_router, "get_drive_service", lambda: service)

    with pytest.raises(HTTPException) as error:
        drive_router.download_file(
            "file_aaaaaaaa",
            ACCOUNT,
            SPACE,
            principal=_principal(),
        )

    assert error.value.status_code == 423
    assert error.value.detail == {
        "code": "drive_revision_quarantined",
        "message": "This file is unavailable until its security scan passes.",
    }


def test_quarantine_capacity_maps_to_retryable_503():
    from app.drive.base import DriveQuarantineCapacityError

    with pytest.raises(HTTPException) as error:
        with drive_router._drive_errors():
            raise DriveQuarantineCapacityError()

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "60"}
    assert error.value.detail["code"] == "drive_quarantine_capacity_exhausted"


def test_security_runtime_status_is_content_free_and_stale_workers_fail_unknown(monkeypatch):
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=ACCOUNT, kind="organization", name="Acme", owner_user_id="user_owner",
    ))
    rows = [ScannerRuntimeStatus(
        tenant_id=ACCOUNT,
        worker_id="worker_aaaaaaaa",
        readiness="ready",
        scanner_engine="clamav",
        scanner_engine_version="1.4.3",
        definition_version="main-63",
        definition_timestamp=now_iso(),
        heartbeat_at="2020-01-01T00:00:00+00:00",
    )]
    service = SimpleNamespace(store=SimpleNamespace(
        list_scanner_runtime_status=lambda *, tenant_id: rows if tenant_id == ACCOUNT else [],
        malware_operational_counts=lambda *, tenant_id: DriveMalwareOperationalCounts(
            pending_count=2,
            quarantine_usage_bytes=42,
            quarantine_reserved_bytes=10,
            quarantined_revision_bytes=32,
        ),
        quarantine_limit_bytes=lambda: 100,
    ))
    monkeypatch.setattr(drive_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(drive_router, "get_drive_service", lambda: service)
    monkeypatch.setattr(
        drive_router,
        "get_settings",
        lambda: SimpleNamespace(
            drive_malware_runtime_stale_seconds=180,
            drive_malware_quarantine_bytes=100,
        ),
    )

    output = drive_router.drive_security_status(ACCOUNT, principal=_principal())

    assert output["readiness"] == "unknown"
    assert output["workers"][0]["readiness"] == "unknown"
    assert output["workers"][0]["stale"] is True
    assert output["pending_count"] == 2
    assert output["quarantine"] == {
        "usage_bytes": 42,
        "reserved_bytes": 10,
        "revision_bytes": 32,
        "limit_bytes": 100,
        "over_capacity": False,
    }
    assert "filename" not in repr(output).lower()
    assert "sha256" not in repr(output).lower()


def test_security_runtime_status_reads_only_the_authorized_requested_account(monkeypatch):
    requested_account = "account_customer"
    principal_account = "account_operator"
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=requested_account,
        kind="organization",
        name="Customer",
        owner_user_id="user_owner",
    ))
    calls: list[tuple[str, str]] = []

    class Store:
        @staticmethod
        def malware_operational_counts(*, tenant_id):
            calls.append(("counts", tenant_id))
            return DriveMalwareOperationalCounts()

        @staticmethod
        def list_scanner_runtime_status(*, tenant_id):
            calls.append(("workers", tenant_id))
            return []

        @staticmethod
        def quarantine_limit_bytes():
            return 100

    monkeypatch.setattr(drive_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(
        drive_router,
        "get_drive_service",
        lambda: SimpleNamespace(store=Store()),
    )
    monkeypatch.setattr(
        drive_router,
        "get_settings",
        lambda: SimpleNamespace(drive_malware_runtime_stale_seconds=180),
    )
    principal = replace(_principal(), tenant_id=principal_account)

    output = drive_router.drive_security_status(requested_account, principal=principal)

    assert output["readiness"] == "unknown"
    assert calls == [
        ("counts", requested_account),
        ("workers", requested_account),
    ]


def test_review_view_authorizes_space_membership_before_reading_pending_metadata(monkeypatch):
    calls: list[tuple[str, str]] = []

    class Service:
        @staticmethod
        def list_pending_review(_principal, *, account_id, space_id):
            calls.append((account_id, space_id))
            return DriveEntryPage()

    monkeypatch.setattr(drive_router, "get_drive_service", Service)

    result = drive_router.list_items(
        account_id=ACCOUNT,
        space_id=SPACE,
        view="review",
        principal=_principal(),
    )

    assert result == {"entries": [], "next_cursor": None}
    assert calls == [(ACCOUNT, SPACE)]

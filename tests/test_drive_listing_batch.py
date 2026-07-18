from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.auth.principal import Principal
from app.drive.base import (
    DRIVE_MALWARE_POLICY_EPOCH,
    MAX_FILE_LIST_DETAIL_BATCH,
    DriveEntryPage,
    DriveFile,
    DriveFileListDetail,
    DriveLimitError,
    DriveMalwareCompletion,
    DriveMalwareScan,
    DriveRevision,
    drive_ingest_idempotency_key,
    drive_ingest_job_id,
)
from app.drive.blobs import LocalDriveBlobStore
from app.drive.memory import MemoryDriveStore
from app.drive.postgres import PostgresDriveStore
from app.drive.scanning import DriveMalwareScanningService
from app.drive.service import DriveService
from app.jobs.base import JOB_DRIVE_FILE_INGEST
from app.jobs.memory import MemoryJobStore
from app.platform.base import Account, Space
from app.platform.memory import MemoryPlatformStore
from app.routers import drive as drive_router
from app.security.policy import Classification
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"
OWNER = "user_owner"
SHA256 = "a" * 64


def _principal(*, categories=None) -> Principal:
    return Principal(
        user_id=OWNER,
        role_id="admin",
        role_label="Admin",
        clearance=Classification.RESTRICTED,
        locations=None,
        categories=categories,
        location_label="all locations",
        tenant_id=ACCOUNT,
    )


def _file(
    index: int,
    *,
    account_id: str = ACCOUNT,
    space_id: str = SPACE,
    category: str = "general",
    index_status: str = "awaiting_scan",
) -> DriveFile:
    return DriveFile(
        id=f"file_{index:08d}",
        tenant_id=account_id,
        account_id=account_id,
        space_id=space_id,
        folder_id="",
        name=f"File {index:08d}.txt",
        classification="internal",
        location="global",
        category=category,
        space_kind="business",
        desired_indexed=True,
        approval_status="not_required",
        index_status=index_status,
        current_revision_id=f"revision_{index:08d}",
        uploaded_by=OWNER,
    )


def _revision(file: DriveFile, *, index: int | None = None) -> DriveRevision:
    suffix = int(file.id.removeprefix("file_")) if index is None else index
    return DriveRevision(
        id=f"revision_{suffix:08d}",
        tenant_id=file.tenant_id,
        account_id=file.account_id,
        space_id=file.space_id,
        file_id=file.id,
        upload_session_id=f"upload_{suffix:08d}",
        storage_key=f"drive/list/{file.id}/revision_{suffix:08d}",
        sha256=SHA256,
        size_bytes=10 + suffix,
        media_type="text/plain",
        original_name=file.name,
        created_by=OWNER,
    )


def _scan(
    revision: DriveRevision,
    *,
    attempt: int = 1,
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    status: str = "pending",
) -> DriveMalwareScan:
    clean = status == "clean"
    return DriveMalwareScan(
        id=f"scan_{revision.id.removeprefix('revision_')}_{policy_epoch}_{attempt}",
        tenant_id=revision.tenant_id,
        account_id=revision.account_id,
        space_id=revision.space_id,
        file_id=revision.file_id,
        revision_id=revision.id,
        revision_sha256=revision.sha256,
        revision_size_bytes=revision.size_bytes,
        policy_epoch=policy_epoch,
        status=status,
        origin="upload",
        attempt_sequence=attempt,
        scanner_engine="clamav" if clean else "",
        scanner_engine_version="1.4.3" if clean else "",
        definition_version="daily-42" if clean else "",
        definition_timestamp="2026-07-18T00:00:00+00:00" if clean else "",
        completed_at="2026-07-18T00:00:01+00:00" if clean else "",
    )


def _service(tmp_path) -> tuple[DriveService, MemoryDriveStore, MemoryJobStore]:
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=ACCOUNT,
        kind="organization",
        name="Acme",
        owner_user_id=OWNER,
    ))
    platform.create_space(Space(
        id=SPACE,
        account_id=ACCOUNT,
        kind="business",
        name="Company",
    ))
    store = MemoryDriveStore(MemoryStore())
    jobs = MemoryJobStore()
    service = DriveService(
        store=store,
        blobs=LocalDriveBlobStore(
            str(tmp_path / "drive"),
            min_free_bytes=0,
            min_free_percent=0,
        ),
        platform_store=platform,
        job_store=jobs,
        settings=SimpleNamespace(
            drive_private_spaces_enabled=False,
            drive_policy_mode="storage_and_indexing",
            job_max_attempts=3,
        ),
    )
    return service, store, jobs


def _insert(store: MemoryDriveStore, file: DriveFile, *, status: str = "pending"):
    stored_file = store.create_file(file)
    revision = store.create_revision(_revision(stored_file))
    scan = store.create_malware_scan(_scan(revision, status=status))
    return stored_file, revision, scan


def test_memory_batch_is_bounded_deduplicated_current_and_one_scan_pass():
    store = MemoryDriveStore(MemoryStore())
    file, revision, first = _insert(store, _file(1))
    latest = store.create_malware_scan(_scan(revision, attempt=2))
    store.create_malware_scan(_scan(
        revision,
        attempt=99,
        policy_epoch=DRIVE_MALWARE_POLICY_EPOCH + 1,
    ))
    other_file = store.create_file(_file(2, account_id="tenant_other", space_id="space_other"))
    other_revision = store.create_revision(_revision(other_file))

    class CountingScans(dict):
        values_calls = 0

        def values(self):
            self.values_calls += 1
            return super().values()

    scans = CountingScans(store._malware_scans)
    store._malware_scans = scans
    details = store.get_file_list_details(
        account_id=ACCOUNT,
        space_id=SPACE,
        revision_ids=(revision.id, revision.id, "revision_missing", other_revision.id),
    )

    assert scans.values_calls == 1
    assert set(details) == {revision.id}
    assert details[revision.id] == DriveFileListDetail(revision, latest)
    assert details[revision.id].malware_scan != first

    replacement = store.create_revision(_revision(file, index=101))
    store.update_file(
        replace(
            file,
            current_revision_id=replacement.id,
            generation=file.generation + 1,
        ),
        expected_generation=file.generation,
    )
    assert store.get_file_list_details(
        account_id=ACCOUNT,
        space_id=SPACE,
        revision_ids=(revision.id,),
    ) == {}

    with pytest.raises(DriveLimitError, match="cannot exceed"):
        store.get_file_list_details(
            account_id=ACCOUNT,
            space_id=SPACE,
            revision_ids=tuple(
                f"revision_{index:08d}"
                for index in range(MAX_FILE_LIST_DETAIL_BATCH + 1)
            ),
        )


def test_memory_batch_drops_mismatched_authoritative_evidence():
    store = MemoryDriveStore(MemoryStore())
    _file_row, revision, scan = _insert(store, _file(1), status="clean")
    store._malware_scans[scan.id] = replace(scan, revision_sha256="b" * 64)

    detail = store.get_file_list_details(
        account_id=ACCOUNT,
        space_id=SPACE,
        revision_ids=(revision.id,),
    )[revision.id]

    assert detail.revision == revision
    assert detail.malware_scan is None


@pytest.mark.parametrize("count", [1, 100])
def test_service_listing_authorizes_once_and_batches_once_without_scalar_reads(
    tmp_path,
    monkeypatch,
    count,
):
    service, store, _jobs = _service(tmp_path)
    for index in range(count):
        _insert(store, _file(index))

    authorization_calls = 0
    batch_calls: list[tuple[str, ...]] = []
    authorize = service.authorize_space
    batch = store.get_file_list_details

    def counted_authorize(principal, account_id, space_id):
        nonlocal authorization_calls
        authorization_calls += 1
        return authorize(principal, account_id, space_id)

    def counted_batch(**kwargs):
        batch_calls.append(tuple(kwargs["revision_ids"]))
        return batch(**kwargs)

    def scalar_read_forbidden(*_args, **_kwargs):
        raise AssertionError("list path used a scalar revision or malware read")

    monkeypatch.setattr(service, "authorize_space", counted_authorize)
    monkeypatch.setattr(store, "get_file_list_details", counted_batch)
    monkeypatch.setattr(store, "get_revision", scalar_read_forbidden)
    monkeypatch.setattr(store, "get_authoritative_malware_scan", scalar_read_forbidden)

    page = service.list_entries(
        _principal(),
        account_id=ACCOUNT,
        space_id=SPACE,
        limit=count,
    )

    assert authorization_calls == 1
    assert len(batch_calls) == 1
    assert len(batch_calls[0]) == count
    assert len(page.files) == count
    assert set(page.file_details) == {row.current_revision_id for row in page.files}


def test_hidden_file_revision_ids_never_enter_the_batch(tmp_path, monkeypatch):
    service, store, _jobs = _service(tmp_path)
    visible, _revision_row, _scan_row = _insert(store, _file(1, category="finance"))
    hidden, _revision_row, _scan_row = _insert(store, _file(2, category="people"))
    requested: list[str] = []
    batch = store.get_file_list_details

    def capture(**kwargs):
        requested.extend(kwargs["revision_ids"])
        return batch(**kwargs)

    monkeypatch.setattr(store, "get_file_list_details", capture)
    page = service.list_entries(
        _principal(categories=frozenset({"finance"})),
        account_id=ACCOUNT,
        space_id=SPACE,
    )

    assert page.files == (visible,)
    assert requested == [visible.current_revision_id]
    assert hidden.current_revision_id not in requested


def test_queued_reconciliation_reuses_batch_and_remains_idempotent(tmp_path, monkeypatch):
    service, store, jobs = _service(tmp_path)
    preseeded_ids: list[str] = []
    for index in range(100):
        file, _revision_row, _scan_row = _insert(
            store,
            _file(index, index_status="queued"),
            status="clean",
        )
        preseeded_ids.append(jobs.enqueue(
            job_id=drive_ingest_job_id(
                file.id,
                file.current_revision_id,
                file.generation,
            ),
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
            idempotency_key=drive_ingest_idempotency_key(
                file.id,
                file.current_revision_id,
                file.generation,
            ),
        ).id)

    batch_calls = 0
    enqueue_batch_sizes: list[int] = []
    batch = store.get_file_list_details
    enqueue_many = jobs.enqueue_many

    def counted_batch(**kwargs):
        nonlocal batch_calls
        batch_calls += 1
        return batch(**kwargs)

    def scalar_read_forbidden(*_args, **_kwargs):
        raise AssertionError("queued reconciliation fell back to scalar metadata reads")

    def scalar_enqueue_forbidden(*_args, **_kwargs):
        raise AssertionError("queued reconciliation fell back to scalar job enqueue")

    def counted_enqueue_many(specs):
        specs = tuple(specs)
        enqueue_batch_sizes.append(len(specs))
        return enqueue_many(specs)

    monkeypatch.setattr(store, "get_file_list_details", counted_batch)
    monkeypatch.setattr(store, "get_revision", scalar_read_forbidden)
    monkeypatch.setattr(store, "get_authoritative_malware_scan", scalar_read_forbidden)
    monkeypatch.setattr(jobs, "enqueue", scalar_enqueue_forbidden)
    monkeypatch.setattr(jobs, "enqueue_many", counted_enqueue_many)

    first = service.list_entries(_principal(), account_id=ACCOUNT, space_id=SPACE)
    second = service.list_entries(_principal(), account_id=ACCOUNT, space_id=SPACE)

    assert len(first.files) == len(second.files) == 100
    assert batch_calls == 2
    assert enqueue_batch_sizes == [100, 100]
    assert len(jobs._jobs) == 100
    assert set(jobs._jobs) == set(preseeded_ids)


def test_listing_repair_and_scan_completion_share_the_exact_job_identity(tmp_path):
    service, store, jobs = _service(tmp_path)
    file, _revision_row, scan = _insert(
        store,
        _file(1, index_status="queued"),
        status="clean",
    )
    expected_id = drive_ingest_job_id(
        file.id,
        file.current_revision_id,
        file.generation,
    )

    service.list_entries(_principal(), account_id=ACCOUNT, space_id=SPACE)

    assert tuple(jobs._jobs) == (expected_id,)
    scanner = object.__new__(DriveMalwareScanningService)
    scanner.job_store = jobs
    scanner.settings = SimpleNamespace(job_max_attempts=3)
    scanner._enqueue_ingestion_if_needed(DriveMalwareCompletion(
        scan=scan,
        file=file,
        ingestion_job_id=expected_id,
        applied=True,
    ))
    assert tuple(jobs._jobs) == (expected_id,)


def test_missing_pending_and_mismatched_details_never_enqueue(tmp_path, monkeypatch):
    service, store, jobs = _service(tmp_path)
    _insert(store, _file(1, index_status="queued"), status="pending")
    _file_row, _revision_row, mismatched = _insert(
        store,
        _file(2, index_status="queued"),
        status="clean",
    )
    store._malware_scans[mismatched.id] = replace(
        mismatched,
        revision_size_bytes=mismatched.revision_size_bytes + 1,
    )
    store.create_file(_file(3, index_status="queued"))

    def scalar_read_forbidden(*_args, **_kwargs):
        raise AssertionError("a batch miss fell back to scalar metadata reads")

    monkeypatch.setattr(store, "get_revision", scalar_read_forbidden)
    monkeypatch.setattr(store, "get_authoritative_malware_scan", scalar_read_forbidden)

    page = service.list_entries(_principal(), account_id=ACCOUNT, space_id=SPACE)

    assert len(page.files) == 3
    assert jobs._jobs == {}


def test_review_list_uses_authorized_batch_snapshot(tmp_path, monkeypatch):
    service, store, _jobs = _service(tmp_path)
    review, _revision_row, _scan_row = _insert(store, replace(
        _file(1),
        approval_status="pending",
        index_status="awaiting_review",
    ))
    authorization_calls = 0
    batch_calls = 0
    authorize = service.authorize_space
    batch = store.get_file_list_details

    def counted_authorize(principal, account_id, space_id):
        nonlocal authorization_calls
        authorization_calls += 1
        return authorize(principal, account_id, space_id)

    def counted_batch(**kwargs):
        nonlocal batch_calls
        batch_calls += 1
        return batch(**kwargs)

    monkeypatch.setattr(service, "authorize_space", counted_authorize)
    monkeypatch.setattr(store, "get_file_list_details", counted_batch)
    page = service.list_pending_review(_principal(), account_id=ACCOUNT, space_id=SPACE)

    assert authorization_calls == 1
    assert batch_calls == 1
    assert page.files == (review,)
    assert set(page.file_details) == {review.current_revision_id}


def test_router_serializers_are_persistence_free_and_fail_closed():
    file = _file(1)
    revision = _revision(file)
    pending = _scan(revision)
    clean = _scan(revision, status="clean")

    missing = drive_router._file_out(file, DriveFileListDetail())
    no_scan = drive_router._file_out(file, DriveFileListDetail(revision=revision))
    old_epoch = drive_router._file_out(
        file,
        DriveFileListDetail(
            revision=revision,
            malware_scan=replace(clean, policy_epoch=DRIVE_MALWARE_POLICY_EPOCH + 1),
        ),
    )
    mismatched = drive_router._file_out(
        file,
        DriveFileListDetail(
            revision=revision,
            malware_scan=replace(clean, revision_sha256="b" * 64),
        ),
    )
    other_file = _file(2)
    other_revision = _revision(other_file)
    cross_file = drive_router._file_out(
        file,
        DriveFileListDetail(
            revision=other_revision,
            malware_scan=_scan(other_revision, status="clean"),
        ),
    )
    quarantined = drive_router._file_out(
        file,
        DriveFileListDetail(revision=revision, malware_scan=pending),
    )
    downloadable = drive_router._file_out(
        file,
        DriveFileListDetail(revision=revision, malware_scan=clean),
    )

    for output in (missing, no_scan, old_epoch, mismatched, cross_file):
        assert output["malware_status"] == "rescan_required"
        assert output["size_bytes"] == 0
        assert output["media_type"] == "application/octet-stream"
        assert output["download_url"] is None
    assert quarantined["malware_status"] == "pending"
    assert quarantined["size_bytes"] == revision.size_bytes
    assert quarantined["download_url"] is None
    assert downloadable["malware_status"] == "clean"
    assert downloadable["download_url"].startswith("/api/drive/files/")

    page = DriveEntryPage(
        files=(file,),
        file_details={revision.id: DriveFileListDetail(revision, clean)},
    )
    assert drive_router._entries_out(page) == [downloadable]
    with pytest.raises(TypeError):
        page.file_details[revision.id] = DriveFileListDetail()  # type: ignore[index]


@pytest.mark.parametrize("count", [1, 100])
def test_postgres_batch_uses_one_scoped_lateral_query(count):
    file = _file(1)
    revision = _revision(file)
    scan = _scan(revision, attempt=2, status="clean")
    row = (
        revision.id,
        revision.tenant_id,
        revision.account_id,
        revision.space_id,
        revision.file_id,
        revision.upload_session_id,
        revision.storage_key,
        revision.sha256,
        revision.size_bytes,
        revision.media_type,
        revision.original_name,
        revision.created_by,
        revision.created_at,
        scan.id,
        scan.tenant_id,
        scan.account_id,
        scan.space_id,
        scan.file_id,
        scan.revision_id,
        scan.revision_sha256,
        scan.revision_size_bytes,
        scan.policy_epoch,
        scan.status,
        scan.origin,
        scan.attempt_sequence,
        scan.consecutive_failures,
        scan.job_id,
        scan.next_attempt_at,
        "",
        "",
        scan.scanner_engine,
        scan.scanner_engine_version,
        scan.definition_version,
        scan.definition_timestamp,
        scan.threat_code,
        scan.error_code,
        scan.started_at,
        scan.completed_at,
        scan.created_at,
        scan.updated_at,
    )
    executions: list[tuple[str, tuple]] = []
    scopes: list[dict] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params):
            executions.append((sql, params))

        @staticmethod
        def fetchall():
            return [row]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        @staticmethod
        def cursor():
            return Cursor()

    store = object.__new__(PostgresDriveStore)

    @contextmanager
    def connection(**scope):
        scopes.append(scope)
        yield Connection()

    store._conn = connection
    revision_ids = tuple(f"revision_{index:08d}" for index in range(count))
    if revision.id not in revision_ids:
        revision_ids = (revision.id, *revision_ids[1:])

    details = store.get_file_list_details(
        account_id=ACCOUNT,
        space_id=SPACE,
        revision_ids=revision_ids,
    )

    assert details[revision.id] == DriveFileListDetail(revision, scan)
    assert scopes == [{"account_id": ACCOUNT, "space_id": SPACE}]
    assert len(executions) == 1
    sql, params = executions[0]
    assert "LEFT JOIN LATERAL" in sql
    assert "current_file.current_revision_id=revision.id" in sql
    assert "current_file.tenant_id=revision.tenant_id" in sql
    assert "evidence.account_id=%s" in sql
    assert "evidence.space_id=%s" in sql
    assert "ORDER BY evidence.attempt_sequence DESC, evidence.id DESC" in sql
    assert len(params[2]) == count

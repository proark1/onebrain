from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth.principal import Principal
from app.drive.access import can_access_file, list_drive_roots, resolve_space_context
from app.drive.base import DriveConflictError, DriveFile, DriveFolder
from app.drive.blobs import LocalDriveBlobStore, drive_storage_key
from app.drive.memory import MemoryDriveStore
from app.drive.service import DriveService
from app.jobs.base import JOB_DRIVE_FILE_INGEST
from app.jobs.memory import MemoryJobStore
from app.platform.base import AccessGroup, Account, LegalHold, Membership, Space
from app.platform.memory import MemoryPlatformStore
from app.security.policy import Classification
from app.store.base import Chunk
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"
OWNER = "user_owner"


def _principal(
    user_id: str = OWNER,
    *,
    role_id: str = "admin",
    categories=None,
    clearance: Classification = Classification.RESTRICTED,
) -> Principal:
    return Principal(
        user_id=user_id,
        role_id=role_id,
        role_label=role_id.title(),
        clearance=clearance,
        locations=None,
        categories=categories,
        location_label="all locations",
        tenant_id=ACCOUNT,
    )


def _platform(*, kind: str = "business") -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id=ACCOUNT, kind="organization", name="Acme", owner_user_id=OWNER))
    platform.create_space(Space(id=SPACE, account_id=ACCOUNT, kind=kind, name="Company"))
    return platform


def _service(tmp_path, *, platform=None, drive_policy_mode="storage_and_indexing"):
    vectors = MemoryStore()
    drive = MemoryDriveStore(vectors)
    jobs = MemoryJobStore()
    blobs = LocalDriveBlobStore(
        str(tmp_path / "d"), min_free_bytes=0, min_free_percent=0,
    )
    settings = SimpleNamespace(
        drive_max_file_bytes=1024,
        drive_upload_session_seconds=3600,
        drive_policy_mode=drive_policy_mode,
        job_max_attempts=3,
    )
    service = DriveService(
        store=drive,
        blobs=blobs,
        platform_store=platform or _platform(),
        job_store=jobs,
        settings=settings,
    )
    return service, vectors, jobs


def _file(**changes) -> DriveFile:
    values = {
        "id": "file_aaaaaaaa",
        "tenant_id": ACCOUNT,
        "account_id": ACCOUNT,
        "space_id": SPACE,
        "folder_id": "",
        "name": "policy.txt",
        "classification": "internal",
        "location": "global",
        "category": "general",
        "space_kind": "business",
        "desired_indexed": True,
        "approval_status": "not_required",
        "index_status": "not_indexed",
        "uploaded_by": OWNER,
    }
    values.update(changes)
    return DriveFile(**values)


def test_drive_roots_are_member_scoped_and_private_space_is_owner_only():
    platform = _platform(kind="personal")
    owner_roots = list_drive_roots(_principal(), platform)
    assert [(row.kind, row.name, row.owner_user_id) for row in owner_roots] == [
        ("personal", "My Drive", OWNER),
    ]

    outsider = _principal("user_outsider", role_id="employee")
    assert list_drive_roots(outsider, platform) == []

    # Multiple active users on a private space are an ambiguous ownership state,
    # and must fail closed instead of turning it into a shared drive.
    platform.upsert_membership(Membership(
        id="membership_aaaaaaaa",
        account_id=ACCOUNT,
        user_id=OWNER,
        role_id="owner",
        space_id=SPACE,
    ))
    platform.upsert_membership(Membership(
        id="membership_bbbbbbbb",
        account_id=ACCOUNT,
        user_id="user_outsider",
        role_id="employee",
        space_id=SPACE,
    ))
    with pytest.raises(HTTPException) as error:
        resolve_space_context(ACCOUNT, SPACE, platform)
    assert error.value.status_code == 409


def test_direct_file_access_uses_same_clearance_location_and_department_labels():
    finance_file = _file(category="finance", classification="confidential")
    assert can_access_file(
        _principal(categories=frozenset({"finance"}), clearance=Classification.CONFIDENTIAL),
        finance_file,
    )
    assert not can_access_file(
        _principal(categories=frozenset({"people"}), clearance=Classification.CONFIDENTIAL),
        finance_file,
    )
    assert not can_access_file(
        _principal(categories=frozenset({"finance"}), clearance=Classification.INTERNAL),
        finance_file,
    )

    private_file = replace(finance_file, space_kind="personal", owner_user_id=OWNER)
    assert can_access_file(_principal(categories=None), private_file)
    assert not can_access_file(_principal("user_outsider", categories=None), private_file)


def test_folder_names_and_breadcrumbs_are_hidden_outside_the_folder_audience(tmp_path):
    service, _vectors, _jobs = _service(tmp_path)
    folder = service.store.create_folder(DriveFolder(
        id="folder_finance",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        parent_id="",
        name="Confidential acquisition",
        default_classification="confidential",
        default_location="global",
        default_category="finance",
        created_by=OWNER,
    ))

    outsider = _principal(categories=frozenset({"people"}), clearance=Classification.RESTRICTED)
    page = service.list_entries(outsider, account_id=ACCOUNT, space_id=SPACE)
    assert page.folders == ()
    with pytest.raises(HTTPException) as error:
        service.breadcrumbs(
            outsider,
            account_id=ACCOUNT,
            space_id=SPACE,
            folder_id=folder.id,
        )
    assert error.value.status_code == 404

    finance = _principal(categories=frozenset({"finance"}), clearance=Classification.RESTRICTED)
    assert service.list_entries(finance, account_id=ACCOUNT, space_id=SPACE).folders == (folder,)


def test_upload_lifecycle_persists_original_and_enqueues_only_identity_metadata(tmp_path):
    service, _vectors, jobs = _service(tmp_path)
    principal = _principal()
    payload = b"OneBrain handbook"
    upload = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        size_bytes=len(payload),
        index_for_ai=True,
        idempotency_key="request-123",
    )
    started, writer = service.begin_upload(principal, upload.id)
    writer.write(payload)
    info = writer.finish()
    uploaded = service.finish_upload_content(principal, started, info, "text/plain")
    completed, file = service.complete_upload(principal, uploaded.id)

    assert completed.status == "completed"
    assert file.index_status == "queued"
    revision = service.store.get_revision(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert revision is not None
    assert b"".join(service.blobs.iter_range(revision.storage_key)) == payload

    assert len(jobs._jobs) == 1
    job = next(iter(jobs._jobs.values()))
    assert job.type == JOB_DRIVE_FILE_INGEST
    assert job.payload == {
        "file_id": file.id,
        "revision_id": file.current_revision_id,
        "generation": file.generation,
    }
    assert jobs.get_file(job.id) is None
    assert all(payload not in str(value).encode() for value in job.payload.values())

    # Completion and session creation are retry-safe and cannot duplicate a file
    # or revision for the same upload/idempotency key.
    replay_upload = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        size_bytes=len(payload),
        index_for_ai=True,
        idempotency_key="request-123",
    )
    replay_completed, replay_file = service.complete_upload(principal, replay_upload.id)
    assert replay_completed.id == completed.id
    assert replay_file.id == file.id
    assert len(service.store.list_revisions(file.id, account_id=ACCOUNT, space_id=SPACE)) == 1


def test_folder_filing_policy_is_inherited_and_children_cannot_widen_it(tmp_path):
    platform = _platform()
    platform.upsert_access_group(AccessGroup(
        id="department_finance",
        account_id=ACCOUNT,
        space_id=SPACE,
        name="Finance",
    ))
    service, _vectors, _jobs = _service(tmp_path, platform=platform)
    principal = _principal(categories=None)
    folder = service.create_folder(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        parent_id="",
        name="Finance vault",
        classification="confidential",
        location="munich",
        category="department_finance",
        index_for_ai=False,
    )

    inherited = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id=folder.id,
        name="forecast.txt",
        size_bytes=10,
        index_for_ai=None,
        idempotency_key="inherited-policy",
    )
    assert (
        inherited.classification,
        inherited.location,
        inherited.category,
        inherited.desired_indexed,
    ) == ("confidential", "munich", "department_finance", False)

    with pytest.raises(PermissionError, match="widen"):
        service.create_upload(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            folder_id=folder.id,
            name="too-wide.txt",
            size_bytes=10,
            index_for_ai=None,
            idempotency_key="widen-policy",
            classification="internal",
        )
    with pytest.raises(PermissionError, match="enable AI"):
        service.create_upload(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            folder_id=folder.id,
            name="ai-enabled.txt",
            size_bytes=10,
            index_for_ai=True,
            idempotency_key="enable-ai-policy",
        )


def test_folder_creation_is_idempotent_for_retried_requests(tmp_path):
    service, _vectors, _jobs = _service(tmp_path)
    principal = _principal()
    first = service.create_folder(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        parent_id="",
        name="Handbooks",
        index_for_ai=True,
        idempotency_key="folder-request-1",
    )
    replay = service.create_folder(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        parent_id="",
        name="Handbooks",
        index_for_ai=True,
        idempotency_key="folder-request-1",
    )

    assert replay == first
    assert service.list_entries(
        principal, account_id=ACCOUNT, space_id=SPACE,
    ).folders == (first,)


def test_file_index_toggle_cannot_override_non_indexed_folder(tmp_path):
    service, vectors, jobs = _service(tmp_path)
    principal = _principal(role_id="employee")
    folder = service.create_folder(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        parent_id="",
        name="Human-only records",
        index_for_ai=False,
    )
    file = service.store.create_file(_file(
        folder_id=folder.id,
        current_revision_id="revision_human_only",
        desired_indexed=False,
        index_status="not_indexed",
    ))

    with pytest.raises(PermissionError, match="enable AI"):
        service.set_indexing(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            file_id=file.id,
            generation=file.generation,
            enabled=True,
        )

    stored = service.store.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)
    assert stored == file
    assert jobs._jobs == {}
    assert vectors.count() == 0


def test_not_indexed_upload_never_enqueues_ai_work(tmp_path):
    service, _vectors, jobs = _service(tmp_path)
    principal = _principal()
    payload = b"private notes"
    upload = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="notes.txt",
        size_bytes=len(payload),
        index_for_ai=False,
        idempotency_key="private-request",
    )
    started, writer = service.begin_upload(principal, upload.id)
    writer.write(payload)
    uploaded = service.finish_upload_content(principal, started, writer.finish(), "text/plain")
    _completed, file = service.complete_upload(principal, uploaded.id)

    assert file.desired_indexed is False
    assert file.index_status == "not_indexed"
    assert jobs._jobs == {}


def test_queued_projection_is_reconciled_idempotently_when_drive_is_browsed(tmp_path):
    service, _vectors, jobs = _service(tmp_path)
    queued = service.store.create_file(_file(
        current_revision_id="revision_aaaaaaaa",
        index_status="queued",
        desired_indexed=True,
    ))

    first = service.list_entries(_principal(), account_id=ACCOUNT, space_id=SPACE)
    second = service.list_entries(_principal(), account_id=ACCOUNT, space_id=SPACE)

    assert first.files == (queued,)
    assert second.files == (queued,)
    assert len(jobs._jobs) == 1
    job = next(iter(jobs._jobs.values()))
    assert job.payload == {
        "file_id": queued.id,
        "revision_id": queued.current_revision_id,
        "generation": queued.generation,
    }


def test_deployment_privacy_mode_keeps_drive_mounted_while_disabling_storage_or_indexing(tmp_path):
    storage_only, _vectors, jobs = _service(tmp_path / "storage", drive_policy_mode="storage_only")
    principal = _principal()
    upload = storage_only.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="policy.txt",
        size_bytes=5,
        index_for_ai=True,
        idempotency_key="storage-only",
    )
    assert upload.desired_indexed is False
    assert jobs._jobs == {}

    disabled, _vectors, _jobs = _service(tmp_path / "disabled", drive_policy_mode="disabled")
    with pytest.raises(PermissionError, match="privacy policy"):
        disabled.create_upload(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            folder_id="",
            name="blocked.txt",
            size_bytes=5,
            index_for_ai=False,
            idempotency_key="disabled",
        )


def test_new_upload_sweeps_expired_staging_sessions_before_capacity_check(tmp_path):
    service, _vectors, _jobs = _service(tmp_path)
    principal = _principal()
    stale = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="abandoned.txt",
        size_bytes=5,
        index_for_ai=False,
        idempotency_key="abandoned",
    )
    uploading, writer = service.begin_upload(principal, stale.id)
    writer.write(b"stale")
    writer.finish()
    service.store.update_upload(replace(
        uploading,
        expires_at="2020-01-01T00:00:00+00:00",
    ))

    service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="fresh.txt",
        size_bytes=5,
        index_for_ai=False,
        idempotency_key="fresh",
    )

    expired = service.store.get_upload(stale.id, tenant_id=ACCOUNT)
    assert expired.status == "expired"
    assert service.blobs.staging_info(stale.id) is None


@pytest.mark.parametrize("operation", ["unindex", "trash"])
def test_access_tightening_removes_existing_vector_projection_immediately(tmp_path, operation):
    service, vectors, _jobs = _service(tmp_path)
    principal = _principal()
    vectors.add([Chunk(
        id="active_doc:0",
        doc_id="active_doc",
        text="confidential projection",
        meta={"tenant_id": ACCOUNT, "status": "approved"},
    )])
    file = service.store.create_file(_file(active_doc_id="active_doc", index_status="indexed"))

    if operation == "unindex":
        stored = service.set_indexing(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            file_id=file.id,
            generation=file.generation,
            enabled=False,
        )
        assert stored.desired_indexed is False
    else:
        stored = service.trash_file(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            file_id=file.id,
            generation=file.generation,
        )
        assert stored.trashed_at

    assert stored.active_doc_id == ""
    assert vectors.count() == 0


def test_download_lookup_is_audited_without_exposing_blob_bytes(tmp_path):
    service, _vectors, _jobs = _service(tmp_path)
    principal = _principal()
    payload = b"download me"
    upload = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="download.txt",
        size_bytes=len(payload),
        index_for_ai=False,
        idempotency_key="download-request",
    )
    started, writer = service.begin_upload(principal, upload.id)
    writer.write(payload)
    uploaded = service.finish_upload_content(principal, started, writer.finish(), "text/plain")
    _completed, file = service.complete_upload(principal, uploaded.id)

    _file_row, revision = service.get_revision_for_download(
        principal, account_id=ACCOUNT, space_id=SPACE, file_id=file.id,
    )
    events = service.platform_store.list_data_access_events(ACCOUNT, SPACE)
    assert [(event.action, event.target_id) for event in events] == [
        ("drive.original.download", file.id),
    ]
    assert events[0].meta == {"revision_id": revision.id, "size_bytes": len(payload)}
    assert payload not in repr(events[0]).encode()


def test_permanent_delete_honors_exact_legal_hold_then_erases_blob_metadata_and_chunks(tmp_path):
    service, vectors, _jobs = _service(tmp_path)
    principal = _principal()
    payload = b"held original"
    upload = service.create_upload(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="held.txt",
        size_bytes=len(payload),
        index_for_ai=False,
        idempotency_key="held-request",
    )
    started, writer = service.begin_upload(principal, upload.id)
    writer.write(payload)
    uploaded = service.finish_upload_content(principal, started, writer.finish(), "text/plain")
    _completed, file = service.complete_upload(principal, uploaded.id)
    revision = service.store.get_revision(file.current_revision_id, account_id=ACCOUNT, space_id=SPACE)
    assert revision is not None and service.blobs.stat(revision.storage_key)

    # Simulate promotion succeeding immediately before a metadata commit crash.
    orphan_upload = "upload_orphaned"
    orphan_revision = "revision_orphaned"
    orphan_key = drive_storage_key(ACCOUNT, ACCOUNT, SPACE, file.id, orphan_revision)
    orphan_writer = service.blobs.begin_staging(orphan_upload, max_bytes=1)
    orphan_writer.write(b"x")
    orphan_writer.finish()
    service.blobs.promote(orphan_upload, orphan_key)

    service.platform_store.create_legal_hold(LegalHold(
        id="hold_revision_raw",
        account_id=ACCOUNT,
        space_id=SPACE,
        subject_ref=revision.id,
        reason="litigation",
        created_by="dpo",
    ))
    with pytest.raises(DriveConflictError, match="legal hold"):
        service.permanently_delete_file(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            file_id=file.id,
            generation=file.generation,
            reason="request",
        )
    service.platform_store.release_legal_hold(ACCOUNT, "hold_revision_raw")

    service.platform_store.create_legal_hold(LegalHold(
        id="hold_aaaaaaaa",
        account_id=ACCOUNT,
        space_id=SPACE,
        subject_ref=f"drive_file:{file.id}",
        reason="litigation",
        created_by="dpo",
    ))
    with pytest.raises(DriveConflictError, match="legal hold"):
        service.permanently_delete_file(
            principal,
            account_id=ACCOUNT,
            space_id=SPACE,
            file_id=file.id,
            generation=file.generation,
            reason="request",
        )
    assert service.blobs.stat(revision.storage_key)
    assert service.store.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)

    service.platform_store.release_legal_hold(ACCOUNT, "hold_aaaaaaaa")
    counts = service.permanently_delete_file(
        principal,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        generation=file.generation,
        reason="request",
    )
    assert counts == {"files": 1, "revisions": 1, "chunks": 0, "blobs_deleted": 2}
    assert service.blobs.stat(revision.storage_key) is None
    assert service.blobs.stat(orphan_key) is None
    assert service.store.get_file(file.id, account_id=ACCOUNT, space_id=SPACE) is None
    assert vectors.count() == 0
    tombstone = service.platform_store.list_tombstones(ACCOUNT)[0]
    assert tombstone.target_ref == f"drive_file:{file.id}"
    assert file.name not in repr(tombstone)

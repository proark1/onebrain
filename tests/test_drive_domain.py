from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from app.drive.base import (
    MAX_PAGE_SIZE,
    DriveConflictError,
    DriveFile,
    DriveFolder,
    DriveGenerationConflict,
    DriveMalwareScan,
    DriveMalwareWorkerStore,
    DriveRevision,
    DriveStore,
    bounded_page_size,
    decode_page_cursor,
    encode_page_cursor,
    ensure_unique_scope,
    normalize_name,
    validate_revision,
)
from app.drive.memory import MemoryDriveStore
from app.store.base import Chunk
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"


def _folder(folder_id: str, name: str, *, parent_id: str = "", **changes) -> DriveFolder:
    values = {
        "id": folder_id,
        "tenant_id": ACCOUNT,
        "account_id": ACCOUNT,
        "space_id": SPACE,
        "parent_id": parent_id,
        "name": name,
        "created_by": "user_owner",
    }
    values.update(changes)
    return DriveFolder(**values)


def _file(file_id: str = "file_aaaaaaaa", *, folder_id: str = "", **changes) -> DriveFile:
    values = {
        "id": file_id,
        "tenant_id": ACCOUNT,
        "account_id": ACCOUNT,
        "space_id": SPACE,
        "folder_id": folder_id,
        "name": "handbook.txt",
        "current_revision_id": "revision_aaaaaaaa",
        "index_status": "queued",
        "uploaded_by": "user_owner",
    }
    values.update(changes)
    return DriveFile(**values)


def test_drive_names_and_scope_records_are_fail_closed():
    assert normalize_name("  Handbook.txt  ") == "Handbook.txt"
    for unsafe in ("", ".", "..", "team/secrets.txt", r"team\secrets.txt"):
        with pytest.raises(ValueError):
            normalize_name(unsafe)

    with pytest.raises(ValueError, match="one tenant"):
        ensure_unique_scope([])
    with pytest.raises(ValueError, match="one tenant"):
        ensure_unique_scope([
            _file(),
            replace(_file("file_bbbbbbbb"), account_id="another_account"),
        ])

    assert bounded_page_size(0) == 1
    assert bounded_page_size(MAX_PAGE_SIZE + 1) == MAX_PAGE_SIZE
    assert decode_page_cursor(encode_page_cursor(25)) == 25
    with pytest.raises(ValueError, match="cursor"):
        decode_page_cursor("folder_aaaaaaaa")


def test_worker_only_malware_capabilities_are_not_in_the_api_store_protocol():
    privileged = {
        "list_expired_uploads_for_maintenance",
        "upsert_scanner_runtime_status",
        "reconcile_quarantine_capacity",
        "list_malware_tenant_ids",
        "begin_malware_scan",
        "complete_malware_scan",
        "reconcile_malware_scans",
        "wake_retryable_malware_scans",
    }

    assert privileged.isdisjoint(DriveStore.__dict__)
    assert privileged <= set(DriveMalwareWorkerStore.__dict__)


def test_revision_contract_rejects_forged_hashes_and_path_names():
    revision = DriveRevision(
        id="revision_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id="file_aaaaaaaa",
        upload_session_id="upload_aaaaaaaa",
        storage_key="drive/a/b/file_aaaaaaaa/revision_aaaaaaaa",
        sha256="a" * 64,
        size_bytes=4,
        media_type="text/plain",
        original_name="safe.txt",
        created_by="user_owner",
    )
    validate_revision(revision)
    with pytest.raises(ValueError, match="sha256"):
        validate_revision(replace(revision, sha256="not-a-digest"))
    with pytest.raises(ValueError, match="path separators"):
        validate_revision(replace(revision, original_name="../secret.txt"))


def test_memory_store_enforces_folder_tree_and_optimistic_generations():
    store = MemoryDriveStore(MemoryStore())
    root = store.create_folder(_folder("folder_aaaaaaaa", "Finance"))
    child = store.create_folder(_folder("folder_bbbbbbbb", "Reports", parent_id=root.id))

    assert [row.id for row in store.breadcrumbs(child.id, account_id=ACCOUNT, space_id=SPACE)] == [
        root.id,
        child.id,
    ]
    with pytest.raises(DriveConflictError, match="already exists"):
        store.create_folder(_folder("folder_cccccccc", "finance"))
    with pytest.raises(DriveConflictError, match="descendant"):
        store.update_folder(
            replace(root, parent_id=child.id, generation=root.generation + 1),
            expected_generation=root.generation,
        )
    with pytest.raises(DriveGenerationConflict):
        store.update_folder(
            replace(child, name="Monthly", generation=child.generation + 1),
            expected_generation=999,
        )


def test_memory_store_pagination_follows_display_order_without_loss():
    store = MemoryDriveStore(MemoryStore())
    # Deliberately make lexical ids disagree with display-name ordering. A cursor
    # must encode the complete ordering key, not compare ids alone.
    store.create_folder(_folder("folder_zzzzzzzz", "Alpha"))
    store.create_folder(_folder("folder_aaaaaaaa", "Zulu"))

    first = store.list_entries(account_id=ACCOUNT, space_id=SPACE, limit=1)
    second = store.list_entries(
        account_id=ACCOUNT,
        space_id=SPACE,
        limit=1,
        cursor=first.next_cursor,
    )

    assert [row.name for row in first.folders + second.folders] == ["Alpha", "Zulu"]
    assert second.next_cursor == ""


def test_projection_publication_is_generation_fenced_and_unpublish_removes_chunks():
    vectors = MemoryStore()
    store = MemoryDriveStore(vectors)
    file = store.create_file(_file())
    revision = store.create_revision(DriveRevision(
        id=file.current_revision_id,
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        upload_session_id="upload_aaaaaaaa",
        storage_key="drive/t/a/s/file_aaaaaaaa/revision_aaaaaaaa",
        sha256="a" * 64,
        size_bytes=4,
        media_type="text/plain",
        original_name=file.name,
        created_by="user_owner",
    ))
    store.create_malware_scan(DriveMalwareScan(
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
        attempt_sequence=1,
        job_id="job_aaaaaaaa",
        scanner_engine="clamav",
        scanner_engine_version="1.4.3",
        definition_version="daily-1",
        definition_timestamp="2026-07-18T00:00:00+00:00",
        completed_at="2026-07-18T00:00:01+00:00",
    ))
    chunk = Chunk(
        id="drive_doc:0",
        doc_id="drive_doc",
        text="Internal handbook",
        meta={"tenant_id": ACCOUNT, "status": "approved"},
        embedding=np.ones(4, dtype=np.float32),
    )

    with pytest.raises(DriveGenerationConflict):
        store.publish_projection(
            file_id=file.id,
            revision_id=file.current_revision_id,
            generation=file.generation + 1,
            account_id=ACCOUNT,
            space_id=SPACE,
            chunks=[chunk],
        )

    published = store.publish_projection(
        file_id=file.id,
        revision_id=file.current_revision_id,
        generation=file.generation,
        account_id=ACCOUNT,
        space_id=SPACE,
        chunks=[chunk],
    )
    assert published.file.active_doc_id == "drive_doc"
    assert vectors.count() == 1

    unpublished = store.unpublish(
        file_id=file.id,
        account_id=ACCOUNT,
        space_id=SPACE,
        generation=file.generation,
    )
    assert unpublished.active_doc_id == ""
    assert unpublished.index_status == "not_indexed"
    assert vectors.count() == 0


def test_memory_revision_creation_matches_postgres_foreign_key_and_retry_semantics():
    store = MemoryDriveStore(MemoryStore())
    revision = DriveRevision(
        id="revision_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id="file_aaaaaaaa",
        upload_session_id="upload_aaaaaaaa",
        storage_key="drive/t/a/s/file_aaaaaaaa/revision_aaaaaaaa",
        sha256="a" * 64,
        size_bytes=1,
        media_type="text/plain",
        original_name="handbook.txt",
        created_by="user_owner",
    )

    with pytest.raises((KeyError, DriveConflictError), match="File|file"):
        store.create_revision(revision)

    store.create_file(_file())
    created = store.create_revision(revision)
    replayed = store.create_revision(revision)
    assert replayed == created
    assert replayed.created_at


def test_clearing_active_projection_during_metadata_update_is_structural():
    vectors = MemoryStore()
    vectors.add([Chunk(
        id="old_doc:0",
        doc_id="old_doc",
        text="must disappear",
        meta={"tenant_id": ACCOUNT, "status": "approved"},
    )])
    store = MemoryDriveStore(vectors)
    file = store.create_file(_file(active_doc_id="old_doc", index_status="indexed"))

    stored = store.update_file(
        replace(file, desired_indexed=False, active_doc_id="", index_status="not_indexed", generation=2),
        expected_generation=1,
    )

    assert stored.active_doc_id == ""
    assert vectors.count() == 0


def test_folder_tree_trash_is_atomic_and_restore_only_revives_the_same_operation():
    vectors = MemoryStore()
    store = MemoryDriveStore(vectors)
    root = store.create_folder(_folder("folder_aaaaaaaa", "Root"))
    child = store.create_folder(_folder("folder_bbbbbbbb", "Child", parent_id=root.id))
    active = store.create_file(_file(folder_id=child.id, active_doc_id="active_doc", index_status="indexed"))
    previously_trashed = store.create_file(_file(
        "file_bbbbbbbb",
        folder_id=child.id,
        desired_indexed=False,
        index_status="not_indexed",
        trashed_at="2026-01-01T00:00:00+00:00",
    ))
    vectors.add([Chunk(
        id="active_doc:0",
        doc_id="active_doc",
        text="must be structurally unpublished",
        meta={
            "tenant_id": ACCOUNT,
            "status": "approved",
            "drive_file_id": active.id,
        },
    )])

    trashed = store.trash_folder_tree(
        root=root,
        expected_generation=root.generation,
        operation_id="trashop_aaaaaaaa",
        timestamp="2026-07-18T00:00:00+00:00",
        folder_generations={root.id: root.generation, child.id: child.generation},
        file_generations={active.id: active.generation},
    )

    assert trashed.root.trash_operation_id == "trashop_aaaaaaaa"
    assert vectors.count() == 0
    trashed_child = store.get_folder(child.id, account_id=ACCOUNT, space_id=SPACE)
    trashed_active = store.get_file(active.id, account_id=ACCOUNT, space_id=SPACE)
    assert trashed_child.trashed_at and trashed_active.trashed_at
    assert store.get_file(previously_trashed.id, account_id=ACCOUNT, space_id=SPACE).trash_operation_id == ""

    restored = store.restore_folder_tree(
        root=trashed.root,
        expected_generation=trashed.root.generation,
        operation_id="trashop_aaaaaaaa",
        folder_generations={
            trashed.root.id: trashed.root.generation,
            trashed_child.id: trashed_child.generation,
        },
        file_generations={trashed_active.id: trashed_active.generation},
        indexing_enabled=True,
    )

    assert restored.root.trashed_at == ""
    assert store.get_folder(child.id, account_id=ACCOUNT, space_id=SPACE).trashed_at == ""
    assert store.get_file(active.id, account_id=ACCOUNT, space_id=SPACE).trashed_at == ""
    still_trashed = store.get_file(previously_trashed.id, account_id=ACCOUNT, space_id=SPACE)
    assert still_trashed.trashed_at == "2026-01-01T00:00:00+00:00"

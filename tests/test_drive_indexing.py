from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.drive.base import DriveFile, DriveMalwareScan, DriveRevision, now_iso
from app.drive.blobs import LocalDriveBlobStore
from app.drive.indexing import handle_drive_index_job
from app.drive.memory import MemoryDriveStore
from app.embeddings.local import LocalEmbedder
from app.jobs.base import JOB_DRIVE_FILE_INGEST, STATUS_RUNNING, Job
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"


def _index_fixture(tmp_path, monkeypatch, *, name="knowledge.txt", payload=b"OneBrain policy handbook"):
    vectors = MemoryStore()
    drive = MemoryDriveStore(vectors)
    blobs = LocalDriveBlobStore(
        str(tmp_path / "drive"), min_free_bytes=0, min_free_percent=0,
    )
    upload_id = "upload_aaaaaaaa"
    file_id = "file_aaaaaaaa"
    revision_id = "revision_aaaaaaaa"
    writer = blobs.begin_staging(upload_id, max_bytes=len(payload))
    writer.write(payload)
    writer.finish()
    storage_key = f"drive/tenant/account/space/{file_id}/{revision_id}"
    blobs.promote(upload_id, storage_key)
    file = drive.create_file(DriveFile(
        id=file_id,
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name=name,
        classification="internal",
        location="global",
        category="general",
        space_kind="business",
        desired_indexed=True,
        approval_status="not_required",
        index_status="queued",
        current_revision_id=revision_id,
        generation=1,
        uploaded_by="user_owner",
    ))
    revision = drive.create_revision(DriveRevision(
        id=revision_id,
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file_id,
        upload_session_id=upload_id,
        storage_key=storage_key,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="text/plain",
        original_name=name,
        created_by="user_owner",
    ))
    timestamp = now_iso()
    drive.create_malware_scan(DriveMalwareScan(
        id="scan_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file_id,
        revision_id=revision_id,
        revision_sha256=revision.sha256,
        revision_size_bytes=revision.size_bytes,
        status="clean",
        scanner_engine="clamav",
        scanner_engine_version="1.4.3",
        definition_version="main-63",
        definition_timestamp=timestamp,
        completed_at=timestamp,
    ))
    job = Job(
        id="job_aaaaaaaa",
        type=JOB_DRIVE_FILE_INGEST,
        status=STATUS_RUNNING,
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        payload={"file_id": file_id, "revision_id": revision_id, "generation": 1},
    )
    monkeypatch.setattr("app.deps.get_drive_store", lambda: drive)
    monkeypatch.setattr("app.deps.get_drive_blob_store", lambda: blobs)
    monkeypatch.setattr("app.deps.get_embedder", lambda: LocalEmbedder(dim=32))
    return drive, vectors, file, job


def _settings(monkeypatch, **changes):
    values = {
        "pii_phase": "dpia_signed",
        "require_approval": False,
        "block_public_on_pii": True,
    }
    values.update(changes)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(**values))


def test_index_job_extracts_embeds_and_publishes_access_labels(tmp_path, monkeypatch):
    drive, vectors, file, job = _index_fixture(tmp_path, monkeypatch)
    _settings(monkeypatch)

    result = handle_drive_index_job(job)

    assert result["status"] == "indexed"
    assert result["chunks"] >= 1
    stored = drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)
    assert stored.index_status == "indexed"
    assert stored.active_doc_id == result["doc_id"]
    assert vectors.count() == result["chunks"]
    exported = vectors.export_documents(ACCOUNT, ACCOUNT, SPACE)
    meta = exported[0]["chunks"][0]["meta"]
    assert meta["drive_file_id"] == file.id
    assert meta["drive_revision_id"] == file.current_revision_id
    assert meta["drive_generation"] == file.generation
    assert meta["account_id"] == ACCOUNT
    assert meta["space_id"] == SPACE
    assert meta["status"] == "approved"


def test_stale_or_disabled_index_work_never_reads_or_publishes(tmp_path, monkeypatch):
    drive, vectors, file, job = _index_fixture(tmp_path, monkeypatch)
    _settings(monkeypatch)

    stale = Job(**{**job.__dict__, "payload": {**job.payload, "generation": 2}})
    assert handle_drive_index_job(stale)["status"] == "stale"
    assert vectors.count() == 0


@pytest.mark.parametrize("status", ["pending", "scanning", "infected", "scan_error", "rescan_required"])
def test_non_clean_current_attempt_blocks_before_blob_read(tmp_path, monkeypatch, status):
    drive, vectors, file, job = _index_fixture(tmp_path, monkeypatch)
    _settings(monkeypatch)
    current = drive.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    drive.create_malware_scan(DriveMalwareScan(
        id=f"scan_{status.replace('_', '')}_2",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        revision_id=file.current_revision_id,
        revision_sha256=current.revision_sha256,
        revision_size_bytes=current.revision_size_bytes,
        status=status,
        attempt_sequence=2,
        threat_code="eicar_test_signature" if status == "infected" else "",
        error_code="scanner_unavailable" if status == "scan_error" else "",
        completed_at=now_iso() if status in {"infected", "scan_error"} else "",
    ))
    monkeypatch.setattr(
        "app.deps.get_drive_blob_store",
        lambda: pytest.fail("quarantined indexing must not open the blob store"),
    )

    result = handle_drive_index_job(job)

    assert result["status"] == "quarantined"
    assert result["malware_status"] == status
    assert vectors.count() == 0

    drive.update_file(
        DriveFile(**{**file.__dict__, "desired_indexed": False, "index_status": "not_indexed"}),
        expected_generation=file.generation,
    )
    assert handle_drive_index_job(job)["status"] == "not_indexed"
    assert vectors.count() == 0


def test_unsupported_original_is_stored_but_has_zero_ai_projection(tmp_path, monkeypatch):
    drive, vectors, file, job = _index_fixture(
        tmp_path, monkeypatch, name="archive.exe", payload=b"MZ\x00binary",
    )
    _settings(monkeypatch)

    result = handle_drive_index_job(job)

    assert result["status"] == "unsupported"
    assert drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE).index_status == "unsupported"
    assert vectors.count() == 0


def test_synthetic_pii_gate_blocks_before_embedding(tmp_path, monkeypatch):
    drive, vectors, file, job = _index_fixture(
        tmp_path,
        monkeypatch,
        payload=b"Contact alice@example.com about payroll.",
    )
    _settings(monkeypatch, pii_phase="synthetic")

    result = handle_drive_index_job(job)

    assert result["status"] == "blocked"
    assert result["reason"] == "personal_data_in_synthetic_mode"
    assert drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE).index_status == "blocked"
    assert vectors.count() == 0


def test_synthetic_pii_gate_scans_filename_before_it_becomes_chunk_metadata(tmp_path, monkeypatch):
    drive, vectors, file, job = _index_fixture(
        tmp_path,
        monkeypatch,
        name="alice@example.com.txt",
        payload=b"Ordinary project notes without personal data in the body.",
    )
    _settings(monkeypatch, pii_phase="synthetic")

    result = handle_drive_index_job(job)

    assert result["status"] == "blocked"
    assert drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE).index_status == "blocked"
    assert vectors.count() == 0


def test_four_eyes_queue_has_no_chunks_until_a_second_person_approves(tmp_path, monkeypatch):
    drive, vectors, file, job = _index_fixture(tmp_path, monkeypatch)
    _settings(monkeypatch, require_approval=True)

    result = handle_drive_index_job(job)

    assert result["status"] == "awaiting_review"
    pending = drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)
    assert pending.approval_status == "pending"
    assert pending.index_status == "awaiting_review"
    assert pending.active_doc_id == ""
    assert vectors.count() == 0


@pytest.mark.parametrize("payload", [b"", b"plain text"])
def test_index_job_rejects_invalid_identity_before_work(tmp_path, monkeypatch, payload):
    _settings(monkeypatch)
    monkeypatch.setattr("app.deps.get_drive_store", lambda: pytest.fail("store must not be opened"))
    job = SimpleNamespace(
        payload={} if not payload else {"file_id": "file_aaaaaaaa", "revision_id": "revision_aaaaaaaa", "generation": 0},
        account_id=ACCOUNT,
        space_id=SPACE,
    )
    with pytest.raises(ValueError, match="identity|generation"):
        handle_drive_index_job(job)

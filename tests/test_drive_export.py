from __future__ import annotations

import io
import json
import tarfile

import pytest

from app.drive.base import DriveFile, DriveRevision
from app.drive.blobs import LocalDriveBlobStore, drive_storage_key
from app.drive.export import (
    DriveExportIntegrityError,
    iter_drive_export_tar,
    prepare_drive_export,
)
from app.drive.memory import MemoryDriveStore
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"


def _archive(tmp_path):
    store = MemoryDriveStore(MemoryStore())
    blobs = LocalDriveBlobStore(
        str(tmp_path / "drive"), min_free_bytes=0, min_free_percent=0,
    )
    file = store.create_file(DriveFile(
        id="file_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="Handbook.txt",
        desired_indexed=False,
        uploaded_by="user_owner",
    ))
    payload = b"portable original"
    revision_id = "revision_aaaaaaaa"
    key = drive_storage_key(ACCOUNT, ACCOUNT, SPACE, file.id, revision_id)
    writer = blobs.begin_staging("upload_aaaaaaaa", max_bytes=len(payload))
    writer.write(payload)
    staged = writer.finish()
    blobs.promote("upload_aaaaaaaa", key)
    store.create_revision(DriveRevision(
        id=revision_id,
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        upload_session_id="upload_aaaaaaaa",
        storage_key=key,
        sha256=staged.sha256,
        size_bytes=staged.size_bytes,
        media_type="text/plain",
        original_name=file.name,
        created_by="user_owner",
    ))
    return store, blobs, payload, key


def test_drive_original_export_is_portable_streamed_tar_with_manifest(tmp_path):
    store, blobs, payload, _key = _archive(tmp_path)
    archive = prepare_drive_export(
        store, blobs, tenant_id=ACCOUNT, account_id=ACCOUNT, space_id=SPACE,
    )
    output = b"".join(iter_drive_export_tar(archive, blobs))

    with tarfile.open(fileobj=io.BytesIO(output), mode="r:") as exported:
        names = exported.getnames()
        assert names[0] == "manifest.json"
        original_path = archive.items[0].archive_path
        assert original_path in names
        manifest = json.load(exported.extractfile("manifest.json"))
        assert manifest["schema"] == "onebrain.drive.originals-export.v1"
        assert manifest["revisions"][0]["archive_path"] == original_path
        assert "storage_key" not in manifest["revisions"][0]
        assert exported.extractfile(original_path).read() == payload


def test_drive_original_export_fails_before_streaming_when_blob_is_missing(tmp_path):
    store, blobs, _payload, key = _archive(tmp_path)
    blobs.delete(key)

    with pytest.raises(DriveExportIntegrityError, match="missing"):
        prepare_drive_export(
            store, blobs, tenant_id=ACCOUNT, account_id=ACCOUNT, space_id=SPACE,
        )

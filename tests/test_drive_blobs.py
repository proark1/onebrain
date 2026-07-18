from __future__ import annotations

import hashlib

import pytest

from app.drive.base import DriveLimitError
from app.drive.blobs import LocalDriveBlobStore


def _store(tmp_path, *, quota_bytes: int = 0) -> LocalDriveBlobStore:
    return LocalDriveBlobStore(
        str(tmp_path / "drive"),
        quota_bytes=quota_bytes,
        min_free_bytes=0,
        min_free_percent=0,
        read_chunk_bytes=64 * 1024,
    )


def test_local_blob_store_streams_hashes_promotes_ranges_and_deletes(tmp_path):
    store = _store(tmp_path)
    payload = b"onebrain-drive-original"
    writer = store.begin_staging("upload_aaaaaaaa", max_bytes=len(payload))
    writer.write(payload[:5])
    writer.write(payload[5:])
    staged = writer.finish()

    assert staged.size_bytes == len(payload)
    assert staged.sha256 == hashlib.sha256(payload).hexdigest()
    assert store.staging_info("upload_aaaaaaaa") == staged

    key = "drive/tenant/account/space/file_aaaaaaaa/revision_aaaaaaaa"
    promoted = store.promote("upload_aaaaaaaa", key)
    assert promoted.key == key
    assert store.staging_info("upload_aaaaaaaa") is None
    assert b"".join(store.iter_range(key, start=3, end=10)) == payload[3:11]
    assert store.stat(key) == promoted
    assert store.delete(key) is True
    assert store.delete(key) is False


def test_blob_writer_aborts_and_removes_partial_file_on_limit(tmp_path):
    store = _store(tmp_path)
    writer = store.begin_staging("upload_bbbbbbbb", max_bytes=4)
    writer.write(b"abc")
    with pytest.raises(DriveLimitError, match="file-size"):
        writer.write(b"de")
    assert store.staging_info("upload_bbbbbbbb") is None
    with pytest.raises(RuntimeError, match="closed"):
        writer.write(b"x")


@pytest.mark.parametrize("key", ["../escape", "drive/../../escape", "drive/.", "C:\\escape"])
def test_blob_paths_cannot_escape_attached_volume(tmp_path, key):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.stat(key)


def test_blob_prefix_delete_is_scoped_and_refuses_storage_root(tmp_path):
    store = _store(tmp_path)
    for upload_id, suffix in (("upload_cccccccc", "rev_cccccccc"), ("upload_dddddddd", "rev_dddddddd")):
        writer = store.begin_staging(upload_id, max_bytes=1)
        writer.write(b"x")
        writer.finish()
        store.promote(upload_id, f"drive/t/a/s/file_aaaaaaaa/{suffix}")

    assert store.delete_prefix("drive/t/a/s/file_aaaaaaaa") == 2
    with pytest.raises(ValueError):
        store.delete_prefix("")


def test_deployment_quota_counts_staged_and_promoted_bytes(tmp_path):
    store = _store(tmp_path, quota_bytes=5)
    writer = store.begin_staging("upload_eeeeeeee", max_bytes=3)
    writer.write(b"abc")
    writer.finish()
    with pytest.raises(DriveLimitError, match="quota"):
        store.ensure_capacity(3)


def test_concurrent_uploads_reserve_declared_capacity_before_bytes_arrive(tmp_path):
    store = _store(tmp_path, quota_bytes=5)
    first = store.begin_staging("upload_ffffffff", max_bytes=3)

    with pytest.raises(DriveLimitError, match="quota"):
        store.begin_staging("upload_gggggggg", max_bytes=3)

    first.abort()
    second = store.begin_staging("upload_gggggggg", max_bytes=3)
    second.abort()

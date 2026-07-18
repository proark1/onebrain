"""Streaming Drive blob protocol and local attached-volume implementation."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Protocol

from app.drive.base import DriveLimitError, validate_opaque_id


@dataclass(frozen=True)
class DriveBlobInfo:
    key: str
    size_bytes: int
    sha256: str


class DriveBlobWriter(Protocol):
    def write(self, data: bytes) -> None: ...
    def finish(self) -> DriveBlobInfo: ...
    def abort(self) -> None: ...


class DriveBlobStore(Protocol):
    def ensure_capacity(self, declared_size: int, *, reserved_bytes: int = 0) -> None: ...
    def begin_staging(self, upload_id: str, *, max_bytes: int) -> DriveBlobWriter: ...
    def staging_info(self, upload_id: str) -> DriveBlobInfo | None: ...
    def promote(self, upload_id: str, permanent_key: str) -> DriveBlobInfo: ...
    def stat(self, key: str) -> DriveBlobInfo | None: ...
    def iter_range(self, key: str, *, start: int = 0, end: int | None = None) -> Iterator[bytes]: ...
    def delete(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def delete_staging(self, upload_id: str) -> bool: ...


def blob_matches_revision(
    info: DriveBlobInfo | None, *, size_bytes: int, sha256: str,
) -> bool:
    """Return whether stored bytes exactly match immutable revision metadata."""

    return bool(
        info
        and info.size_bytes == size_bytes
        and info.sha256 == sha256
    )


def drive_scope_prefix(tenant_id: str, account_id: str, space_id: str = "") -> str:
    """Return the opaque, prefix-deletable object namespace for one scope."""

    tenant = (tenant_id or "").strip()
    account = (account_id or "").strip()
    if not tenant or not account:
        raise ValueError("Drive storage tenant and account scopes cannot be empty.")
    # Hash the tenant/account tuple as one 96-bit compartment. This remains
    # collision-resistant while leaving enough Windows path headroom for local
    # development and long pytest/worktree roots.
    values = ["drive", _scope_token(f"{tenant}\0{account}", length=24)]
    if space_id:
        values.append(_scope_token(space_id, length=20))
    return "/".join(values)


def drive_storage_key(
    tenant_id: str, account_id: str, space_id: str, file_id: str, revision_id: str,
) -> str:
    validate_opaque_id(file_id, field_name="file_id")
    validate_opaque_id(revision_id, field_name="revision_id")
    return "/".join((drive_scope_prefix(tenant_id, account_id, space_id), file_id, revision_id))


def _scope_token(value: str, *, length: int = 24) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("Drive storage scope cannot be empty.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length]


class _LocalWriter:
    def __init__(
        self,
        path: Path,
        key: str,
        max_bytes: int,
        *,
        on_progress: Callable[[int], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ):
        self._path = path
        self._key = key
        self._max_bytes = max_bytes
        self._size = 0
        self._hash = hashlib.sha256()
        self._handle: BinaryIO | None = None
        self._finished = False
        self._on_progress = on_progress
        self._on_close = on_close
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(path, "xb")
        if os.name != "nt":
            os.chmod(path, 0o600)

    def write(self, data: bytes) -> None:
        if self._finished or self._handle is None:
            raise RuntimeError("Drive upload writer is closed.")
        if not data:
            return
        next_size = self._size + len(data)
        if next_size > self._max_bytes:
            self.abort()
            raise DriveLimitError("Drive upload exceeds the configured file-size limit.")
        self._handle.write(data)
        self._hash.update(data)
        self._size = next_size
        if self._on_progress:
            self._on_progress(self._size)

    def finish(self) -> DriveBlobInfo:
        if self._finished:
            raise RuntimeError("Drive upload writer is already closed.")
        if self._handle is None:
            raise RuntimeError("Drive upload writer is unavailable.")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._handle = None
        self._finished = True
        self._release_reservation()
        return DriveBlobInfo(self._key, self._size, self._hash.hexdigest())

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self._finished = True
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._release_reservation()

    def _release_reservation(self) -> None:
        callback, self._on_close = self._on_close, None
        if callback:
            callback()


class LocalDriveBlobStore:
    def __init__(
        self,
        root: str,
        *,
        quota_bytes: int = 0,
        min_free_bytes: int = 512 * 1024 * 1024,
        min_free_percent: float = 5.0,
        read_chunk_bytes: int = 1024 * 1024,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "staging").mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for directory in (self.root, self.root / "staging", self.root / "objects"):
                os.chmod(directory, 0o700)
        self.quota_bytes = max(0, int(quota_bytes))
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.min_free_percent = max(0.0, min(float(min_free_percent), 100.0))
        self.read_chunk_bytes = max(64 * 1024, int(read_chunk_bytes))
        self._capacity_lock = threading.RLock()
        self._reservations: dict[str, int] = {}

    def ensure_capacity(self, declared_size: int, *, reserved_bytes: int = 0) -> None:
        declared_size = int(declared_size)
        if declared_size <= 0:
            raise ValueError("Drive upload size must be positive.")
        usage = shutil.disk_usage(self.root)
        remaining = usage.free - max(0, int(reserved_bytes)) - declared_size
        required_by_percent = int(usage.total * (self.min_free_percent / 100.0))
        if remaining < max(self.min_free_bytes, required_by_percent):
            raise DriveLimitError("Drive storage does not have enough reserved free space.")
        if self.quota_bytes:
            used = self._tree_size(self.root / "objects") + self._tree_size(self.root / "staging")
            if used + max(0, int(reserved_bytes)) + declared_size > self.quota_bytes:
                raise DriveLimitError("Drive deployment quota would be exceeded.")

    def begin_staging(self, upload_id: str, *, max_bytes: int) -> DriveBlobWriter:
        validate_opaque_id(upload_id, field_name="upload_id")
        path = self._staging_path(upload_id)
        declared = max(1, int(max_bytes))
        with self._capacity_lock:
            if path.exists():
                raise FileExistsError("Upload content was already started.")
            self.ensure_capacity(
                declared,
                reserved_bytes=sum(self._reservations.values()),
            )
            writer = _LocalWriter(
                path,
                f"staging/{upload_id}",
                declared,
                on_progress=lambda size: self._update_reservation(
                    upload_id, declared - size,
                ),
                on_close=lambda: self._release_reservation(upload_id),
            )
            self._reservations[upload_id] = declared
            return writer

    def staging_info(self, upload_id: str) -> DriveBlobInfo | None:
        validate_opaque_id(upload_id, field_name="upload_id")
        path = self._staging_path(upload_id)
        if not path.is_file():
            return None
        return DriveBlobInfo(f"staging/{upload_id}", path.stat().st_size, self._sha256(path))

    def promote(self, upload_id: str, permanent_key: str) -> DriveBlobInfo:
        validate_opaque_id(upload_id, field_name="upload_id")
        source = self._staging_path(upload_id)
        destination = self._object_path(permanent_key)
        self._ensure_secure_directory(destination.parent)
        if destination.exists():
            if source.exists():
                source.unlink()
            return DriveBlobInfo(permanent_key, destination.stat().st_size, self._sha256(destination))
        if not source.is_file():
            raise FileNotFoundError("Staged Drive upload is missing.")
        os.replace(source, destination)
        if os.name != "nt":
            os.chmod(destination, 0o600)
        self._fsync_directory(destination.parent)
        return DriveBlobInfo(permanent_key, destination.stat().st_size, self._sha256(destination))

    def stat(self, key: str) -> DriveBlobInfo | None:
        path = self._object_path(key)
        if not path.is_file():
            return None
        return DriveBlobInfo(key, path.stat().st_size, self._sha256(path))

    def iter_range(self, key: str, *, start: int = 0, end: int | None = None) -> Iterator[bytes]:
        path = self._object_path(key)
        size = path.stat().st_size
        start = max(0, int(start))
        final = size - 1 if end is None else min(int(end), size - 1)
        if start >= size or final < start:
            raise ValueError("Requested Drive blob range is unsatisfiable.")
        remaining = final - start + 1
        with open(path, "rb") as handle:
            handle.seek(start)
            while remaining:
                chunk = handle.read(min(self.read_chunk_bytes, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    def delete(self, key: str) -> bool:
        path = self._object_path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        self._prune_empty(path.parent)
        return True

    def delete_prefix(self, prefix: str) -> int:
        path = self._object_path(prefix, allow_prefix=True)
        if path == self.root or path == self.root / "objects":
            raise ValueError("Refusing broad Drive blob deletion.")
        if path.is_file():
            path.unlink()
            return 1
        if not path.is_dir():
            return 0
        count = sum(1 for item in path.rglob("*") if item.is_file())
        shutil.rmtree(path)
        self._prune_empty(path.parent)
        return count

    def delete_staging(self, upload_id: str) -> bool:
        validate_opaque_id(upload_id, field_name="upload_id")
        self._release_reservation(upload_id)
        try:
            self._staging_path(upload_id).unlink()
            return True
        except FileNotFoundError:
            return False

    def _update_reservation(self, upload_id: str, remaining: int) -> None:
        with self._capacity_lock:
            if upload_id in self._reservations:
                self._reservations[upload_id] = max(0, int(remaining))

    def _release_reservation(self, upload_id: str) -> None:
        with self._capacity_lock:
            self._reservations.pop(upload_id, None)

    def _staging_path(self, upload_id: str) -> Path:
        return self.root / "staging" / f"{upload_id}.part"

    def _object_path(self, key: str, *, allow_prefix: bool = False) -> Path:
        parts = [part for part in (key or "").replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} or not re_safe_part(part) for part in parts):
            raise ValueError("Drive storage key is invalid.")
        path = (self.root / "objects" / Path(*parts)).resolve()
        objects_root = (self.root / "objects").resolve()
        try:
            path.relative_to(objects_root)
        except ValueError as exc:
            raise ValueError("Drive storage key escapes the storage root.") from exc
        if not allow_prefix and path == objects_root:
            raise ValueError("Drive storage key is too broad.")
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _tree_size(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _prune_empty(self, path: Path) -> None:
        boundary = (self.root / "objects").resolve()
        current = path.resolve()
        while current != boundary:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _ensure_secure_directory(self, path: Path) -> None:
        """Create every object-key directory with an explicit private mode."""

        boundary = (self.root / "objects").resolve()
        target = path.resolve()
        try:
            relative = target.relative_to(boundary)
        except ValueError as exc:
            raise ValueError("Drive object directory escapes the storage root.") from exc
        current = boundary
        for part in relative.parts:
            current = current / part
            current.mkdir(exist_ok=True)
            if os.name != "nt":
                os.chmod(current, 0o700)


def re_safe_part(value: str) -> bool:
    return bool(value) and len(value) <= 128 and all(char.isalnum() or char in "_-" for char in value)

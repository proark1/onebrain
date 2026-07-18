"""Portable, streaming Drive-original export independent from HTTP transport."""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from typing import Iterator

from app.drive.base import normalize_name


TAR_BLOCK_SIZE = 512


class DriveExportIntegrityError(RuntimeError):
    """The metadata and durable original store disagree."""


@dataclass(frozen=True)
class DriveExportItem:
    archive_path: str
    storage_key: str
    size_bytes: int
    sha256: str
    media_type: str


@dataclass(frozen=True)
class DriveExportArchive:
    manifest: dict
    items: tuple[DriveExportItem, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)


def prepare_drive_export(
    drive_store,
    blobs,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str = "",
) -> DriveExportArchive:
    """Resolve and integrity-check every original before response streaming starts."""

    scope = drive_store.export_scope(
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
    )
    files = {row["id"]: dict(row) for row in scope.get("files", [])}
    revisions = sorted(
        (dict(row) for row in scope.get("revisions", [])),
        key=lambda row: (row.get("file_id", ""), row.get("created_at", ""), row.get("id", "")),
    )
    items: list[DriveExportItem] = []
    manifest_revisions: list[dict] = []
    for revision in revisions:
        file = files.get(revision.get("file_id", ""))
        if not file:
            raise DriveExportIntegrityError("Drive export contains a revision without its file.")
        storage_key = str(revision.get("storage_key") or "")
        info = blobs.stat(storage_key)
        if (
            not info
            or info.size_bytes != int(revision.get("size_bytes") or 0)
            or info.sha256 != revision.get("sha256")
        ):
            raise DriveExportIntegrityError(
                f"Drive original {revision.get('id', '')} is missing or failed integrity validation."
            )
        try:
            original_name = normalize_name(str(revision.get("original_name") or file.get("name") or "original"))
        except ValueError:
            original_name = "original"
        archive_path = "/".join((
            "files",
            str(file["id"]),
            str(revision["id"]),
            original_name,
        ))
        items.append(DriveExportItem(
            archive_path=archive_path,
            storage_key=storage_key,
            size_bytes=info.size_bytes,
            sha256=info.sha256,
            media_type=str(revision.get("media_type") or "application/octet-stream"),
        ))
        public_revision = {
            key: value for key, value in revision.items() if key != "storage_key"
        }
        public_revision["archive_path"] = archive_path
        manifest_revisions.append(public_revision)

    manifest = {
        "schema": "onebrain.drive.originals-export.v1",
        "tenant_id": tenant_id,
        "account_id": account_id,
        "space_id": space_id,
        "files": [files[key] for key in sorted(files)],
        "revisions": manifest_revisions,
        "folders": sorted(
            (dict(row) for row in scope.get("folders", [])),
            key=lambda row: (row.get("space_id", ""), row.get("parent_id", ""), row.get("id", "")),
        ),
    }
    return DriveExportArchive(manifest=manifest, items=tuple(items))


def iter_drive_export_tar(archive: DriveExportArchive, blobs) -> Iterator[bytes]:
    """Yield a POSIX/PAX tar without buffering account originals in memory."""

    manifest = json.dumps(
        archive.manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    yield _tar_header("manifest.json", len(manifest), media_type="application/json")
    yield manifest
    yield _padding(len(manifest))

    for item in archive.items:
        yield _tar_header(item.archive_path, item.size_bytes, media_type=item.media_type)
        emitted = 0
        for chunk in blobs.iter_range(item.storage_key):
            emitted += len(chunk)
            yield chunk
        if emitted != item.size_bytes:
            raise DriveExportIntegrityError(
                f"Drive original changed while exporting: {item.archive_path}."
            )
        yield _padding(emitted)
    yield b"\0" * (TAR_BLOCK_SIZE * 2)


def _tar_header(name: str, size: int, *, media_type: str) -> bytes:
    info = tarfile.TarInfo(name=name)
    info.size = int(size)
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {"onebrain.media_type": media_type}
    return info.tobuf(format=tarfile.PAX_FORMAT)


def _padding(size: int) -> bytes:
    remainder = int(size) % TAR_BLOCK_SIZE
    return b"" if remainder == 0 else b"\0" * (TAR_BLOCK_SIZE - remainder)

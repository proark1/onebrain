"""Build replaceable Drive stores while keeping Drive always in Core."""

from __future__ import annotations

import os

from app.config import Settings
from app.drive.base import DriveMalwareWorkerStore, DriveStore
from app.drive.blobs import LocalDriveBlobStore


def drive_data_root(settings: Settings) -> str:
    configured = (settings.drive_data_dir or "").strip()
    return configured or os.path.join(settings.data_dir, "drive")


def build_drive_blob_store(settings: Settings):
    return LocalDriveBlobStore(
        drive_data_root(settings),
        quota_bytes=settings.drive_quota_bytes,
        min_free_bytes=settings.drive_min_free_bytes,
        min_free_percent=settings.drive_min_free_percent,
    )


def build_drive_store(
    settings: Settings,
    vector_store,
    *,
    dim: int,
) -> DriveStore:
    return _build_drive_store(settings, vector_store, dim=dim, worker_dsn="")


def build_drive_worker_store(
    settings: Settings,
    vector_store,
    *,
    dim: int,
    worker_dsn: str,
) -> DriveMalwareWorkerStore:
    return _build_drive_store(settings, vector_store, dim=dim, worker_dsn=worker_dsn)


def _build_drive_store(
    settings: Settings,
    vector_store,
    *,
    dim: int,
    worker_dsn: str,
) -> DriveMalwareWorkerStore:
    if settings.vector_store == "pgvector":
        from app.drive.postgres import PostgresDriveStore

        return PostgresDriveStore(
            settings.pg_database_url,
            dim=dim,
            worker_dsn=worker_dsn,
        )

    from app.drive.memory import MemoryDriveStore

    path = os.path.join(settings.data_dir, "drive.json") if settings.persist else None
    return MemoryDriveStore(
        vector_store,
        persist_path=path,
        quarantine_limit_bytes=settings.drive_malware_quarantine_bytes,
    )

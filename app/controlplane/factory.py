"""Build the operator control-plane store."""

from __future__ import annotations

import os

from app.config import Settings


def build_control_plane_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.controlplane.postgres import PostgresControlPlaneStore

        return PostgresControlPlaneStore(settings.database_url)

    from app.controlplane.memory import MemoryControlPlaneStore

    path = os.path.join(settings.data_dir, "control_plane.json") if settings.persist else None
    return MemoryControlPlaneStore(persist_path=path)

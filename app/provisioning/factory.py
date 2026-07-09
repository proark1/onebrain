"""Build the configured provisioning-run store."""

from __future__ import annotations

import os

from app.config import Settings


def build_provisioning_run_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.provisioning.runs import PostgresProvisioningRunStore

        return PostgresProvisioningRunStore(settings.database_url)

    from app.provisioning.runs import MemoryProvisioningRunStore

    path = os.path.join(settings.data_dir, "provisioning_runs.json") if settings.persist else None
    return MemoryProvisioningRunStore(persist_path=path)

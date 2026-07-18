"""Build the configured provisioning-run store."""

from __future__ import annotations

import os

from app.config import Settings


def build_provisioning_run_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.provisioning.runs import PostgresProvisioningRunStore

        # Provisioning state is Mission Control metadata. Its callbacks and
        # bootstrap exchanges resolve opaque run/deployment identifiers before
        # an account scope is known, so the store must use the explicit,
        # role-bound operator DSN rather than the tenant-scoped API login.
        return PostgresProvisioningRunStore(
            settings.pg_database_url,
            operator_dsn=settings.pg_operator_database_url,
        )

    from app.provisioning.runs import MemoryProvisioningRunStore

    path = os.path.join(settings.data_dir, "provisioning_runs.json") if settings.persist else None
    return MemoryProvisioningRunStore(persist_path=path)

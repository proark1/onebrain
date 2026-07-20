"""Build the platform store."""

from __future__ import annotations

import os

from app.config import Settings


def _bootstrap_account_id(settings: Settings) -> str:
    """The single account a customer box owns, or "" for MC and local runs.

    Startup validates the descriptor and fails closed on a malformed one, so a
    decode failure here is never the place to report it.
    """
    from app.provisioning.customer_bootstrap import decode_customer_bootstrap

    try:
        descriptor = decode_customer_bootstrap(settings.customer_bootstrap)
    except ValueError:
        return ""
    return descriptor.account_id if descriptor else ""


def build_platform_store(settings: Settings):
    if settings.vector_store == "pgvector":
        from app.platform.postgres import PostgresPlatformStore

        return PostgresPlatformStore(
            settings.pg_database_url,
            operator_dsn=settings.pg_operator_database_url,
            bootstrap_account_id=_bootstrap_account_id(settings),
        )

    from app.platform.memory import MemoryPlatformStore

    path = os.path.join(settings.data_dir, "platform.json") if settings.persist else None
    return MemoryPlatformStore(persist_path=path)

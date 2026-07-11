"""Runtime configuration.

Everything is env-driven with sensible, zero-cost defaults so the app runs
locally with no API keys and no database. Flip the providers to move to
production without touching the rest of the code.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


def _under_pytest() -> bool:
    """True when running inside pytest. `pytest` is always imported by the time
    our modules are collected, so this is reliable even at import time — before
    any test body runs."""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _load_dotenv(path: str = ".env") -> None:
    """Load .env into the environment so provider keys (e.g. MISTRAL_API_KEY)
    reach LiteLLM, not just our own ONEBRAIN_ settings. Existing env vars win."""
    # Never auto-load a developer .env into a test run. Doing so silently pointed
    # the suite at the real PostgreSQL DSN and async worker and let tests write to
    # live data. Under pytest we fall back to the in-code defaults (memory store,
    # local providers, synchronous ingestion). Set ONEBRAIN_LOAD_DOTENV=1 to opt a
    # specific test run back in.
    if _under_pytest() and os.environ.get("ONEBRAIN_LOAD_DOTENV") != "1":
        return
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _dsn_label(dsn: str) -> str:
    """host/database only — never credentials — for safe error messages."""
    try:
        parts = urlsplit(dsn)
        return f"{parts.hostname or '?'}/{(parts.path or '').lstrip('/') or '?'}"
    except Exception:
        return "<unparseable dsn>"


def _looks_like_test_dsn(dsn: str) -> bool:
    try:
        return "test" in (urlsplit(dsn).path or "").lstrip("/").lower()
    except Exception:
        return False


def _guard_pytest_dsn(dsn: str) -> None:
    """Fail closed if a test run is about to connect to a non-test database.

    The primary defense is not loading .env under pytest; this backstops the case
    where ONEBRAIN_VECTOR_STORE=pgvector and a real DSN are set directly in the
    test environment."""
    if not _under_pytest():
        return
    dsn = (dsn or "").strip()
    if not dsn or os.environ.get("ONEBRAIN_ALLOW_NONTEST_DB") == "1" or _looks_like_test_dsn(dsn):
        return
    raise RuntimeError(
        f"Refusing to use a non-test PostgreSQL database during tests: {_dsn_label(dsn)}. "
        "Tests default to the in-memory store; to target a disposable test database "
        "name it with 'test' or set ONEBRAIN_ALLOW_NONTEST_DB=1."
    )


_load_dotenv()


class Settings(BaseSettings):
    # Under pytest we do NOT read .env (neither here nor via _load_dotenv above):
    # a test run must never inherit the real DSN or provider config. See
    # _under_pytest — pytest is already imported when this class is defined.
    model_config = SettingsConfigDict(
        env_file=None if _under_pytest() else ".env",
        env_prefix="ONEBRAIN_",
        extra="ignore",
    )

    # Providers — "local" variants need no API key; swap to real for production.
    embeddings_provider: str = "local"   # local | litellm
    llm_provider: str = "local"          # local | litellm
    vector_store: str = "memory"         # memory | pgvector
    environment: str = "local"           # local | development | staging | production

    # Retrieval
    top_k: int = 8
    retrieval_min_score: float = 0.05
    embedding_dim: int = 256             # dimension of the local hashing embedder

    # Local persistence (so uploads survive a restart with the memory store)
    data_dir: str = ".onebrain_data"
    persist: bool = True
    seed_sample_data: bool = True

    # LiteLLM — only used when a provider is set to "litellm".
    # Model strings follow LiteLLM's "<provider>/<model>" format, so switching
    # provider (gemini / mistral / anthropic / openai) is a one-line change.
    litellm_model: str = "gemini/gemini-2.5-flash"
    litellm_embedding_model: str = "gemini/gemini-embedding-001"

    # pgvector — only used when vector_store = "pgvector"
    database_url: str = ""
    migration_database_url: str = ""
    # Privileged DSN for cross-account operator/admin reads (list all accounts,
    # pending queues, dashboards). Must authenticate as a role that owns the
    # platform tables / bypasses RLS. Falls back to the migration then app DSN.
    operator_database_url: str = ""

    # External customer provisioning through GitHub Actions.
    github_owner: str = ""
    github_repo: str = ""
    github_workflow: str = "provision-customer.yml"
    github_ref: str = "main"
    github_dispatch_token: str = ""
    provisioning_callback_key_id: str = ""
    provisioning_callback_key_hash: str = ""
    secret_encryption_key: str = ""
    secret_encryption_key_version: str = "v1"
    bootstrap_secret_ttl_seconds: int = 3600
    postgres_app_role: str = ""
    postgres_service_role: str = ""
    rls_enforced: bool = False

    # Background jobs. Postgres mode defaults to async ingestion because OCR,
    # embedding, and provider calls should not hold request workers open.
    async_ingestion: Optional[bool] = None
    worker_poll_seconds: float = 2.0
    worker_batch_size: int = 1
    job_max_attempts: int = 3

    # Auth — sign session cookies. SET A STRONG SECRET IN PRODUCTION.
    auth_secret: str = "dev-insecure-change-me"
    session_days: int = 7
    seed_demo_users: bool = True
    cookie_secure: bool = True    # set false only for local http dev

    # Production admin bootstrap — the SAFE login path (no shared/default password).
    # Set both to have a real admin account ensured on any stack, incl. production.
    admin_email: str = ""
    admin_password: str = ""

    # Shared-password demo accounts (email + "onebrain2026") are convenient for a
    # local demo but must NEVER auto-seed on a real deployment. They seed only on a
    # fully-local stack unless this is explicitly opted into.
    allow_demo_users: bool = False

    # Cap on request body size (bytes) — a cheap DoS/edge guard. 50 MB leaves room
    # for large document/PDF uploads while rejecting absurd payloads.
    max_body_bytes: int = 50 * 1024 * 1024

    # Product UI handoff. FastAPI is API-first; Next.js owns the browser
    # console. The old static UI is available only when explicitly enabled.
    admin_ui_url: str = ""
    legacy_static_ui_enabled: bool = False

    # Per-tier LLM routing (Schrems II): CONFIDENTIAL/RESTRICTED answers route to an
    # EU-sovereign endpoint; PUBLIC/INTERNAL use the default model. Leave
    # sovereign_llm_model empty to disable routing (everything uses the default).
    sovereign_llm_model: str = ""              # e.g. mistral/mistral-large-latest
    sovereign_min_tier: str = "confidential"   # min classification that must route sovereign
    sovereign_required: bool = False           # fail closed if sensitive + no sovereign endpoint

    # Login throttle — per-account brute-force / credential-stuffing lockout.
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900

    # --- Mission Control (fleet control plane) ---
    # operator_mode: this deployment IS Mission Control — it ingests fleet
    # heartbeats and serves the operator/fleet surface. operator_console:
    # whether the operator/fleet read surface is exposed at all (a
    # customer-serving deployment sets this false so its admins can't see fleet
    # state). A deployment reports to Mission Control when fleet_url + fleet_key
    # are set.
    operator_mode: bool = False
    operator_console: bool = True
    deployment_id: str = ""              # this deployment's control-plane id (for its heartbeat)
    fleet_url: str = ""                  # Mission Control base URL the reporter POSTs to
    fleet_key: str = ""                  # this deployment's fleet heartbeat key (fk_...)
    fleet_report_seconds: int = 60       # how often the reporter posts a heartbeat
    fleet_missed_heartbeat_seconds: int = 600   # watchdog: alert when older than this
    fleet_target_version: str = ""       # expected fleet bundle version (drift alerting)

    # Service surface — per-key rate limit (metered LLM/embedding endpoints) and a
    # cap on how many active keys one tenant may hold.
    service_rate_limit: int = 60
    service_rate_window_seconds: int = 60
    max_service_keys_per_tenant: int = 50

    # Publication lifecycle (human-error firewall).
    #   require_approval:     every upload lands in quarantine until a second,
    #                         sufficiently-cleared person approves it (four-eyes).
    #   block_public_on_pii:  a PUBLIC upload with detected PII is auto-quarantined
    #                         even when require_approval is off (on by default).
    require_approval: bool = False
    block_public_on_pii: bool = True

    # Synthetic-data phase gate. While "synthetic", any upload in which the PII
    # scanner detects real personal data is REFUSED — a code-enforced version of
    # "synthetic data only until the DPIA is signed". Flip to "dpia_signed" once
    # the DPIA (and EU-sovereign routing) are in place to allow real PII.
    pii_phase: str = "synthetic"   # synthetic | dpia_signed

    @property
    def is_local_stack(self) -> bool:
        """True only when every provider is the keyless local variant (dev/test)."""
        return (
            self.llm_provider == "local"
            and self.embeddings_provider == "local"
            and self.vector_store == "memory"
        )

    @property
    def use_async_ingestion(self) -> bool:
        if self.async_ingestion is not None:
            return bool(self.async_ingestion)
        return self.vector_store == "pgvector"

    @property
    def is_production_like(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production", "staging"}

    @property
    def pg_database_url(self) -> str:
        """Postgres DSN for building a store, guarded so a test run can never
        connect to a non-test database. In production (not under pytest) this
        returns database_url unchanged."""
        _guard_pytest_dsn(self.database_url)
        return self.database_url

    @property
    def pg_operator_database_url(self) -> str:
        """Privileged DSN for cross-account operator/admin queries, guarded like
        pg_database_url. Falls back to the migration (owner) DSN, then the app
        DSN — so a single-DSN deployment keeps working, though it should point
        this at a distinct privileged role to close the RLS admin bypass."""
        dsn = self.operator_database_url.strip() or self.migration_database_url.strip() or self.database_url
        _guard_pytest_dsn(dsn)
        return dsn


@lru_cache
def get_settings() -> Settings:
    return Settings()

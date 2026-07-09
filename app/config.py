"""Runtime configuration.

Everything is env-driven with sensible, zero-cost defaults so the app runs
locally with no API keys and no database. Flip the providers to move to
production without touching the rest of the code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_dotenv(path: str = ".env") -> None:
    """Load .env into the environment so provider keys (e.g. MISTRAL_API_KEY)
    reach LiteLLM, not just our own ONEBRAIN_ settings. Existing env vars win."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ONEBRAIN_", extra="ignore")

    # Providers — "local" variants need no API key; swap to real for production.
    embeddings_provider: str = "local"   # local | litellm
    llm_provider: str = "local"          # local | litellm
    vector_store: str = "memory"         # memory | pgvector

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


@lru_cache
def get_settings() -> Settings:
    return Settings()

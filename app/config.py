"""Runtime configuration.

Everything is env-driven with sensible, zero-cost defaults so the app runs
locally with no API keys and no database. Flip the providers to move to
production without touching the rest of the code.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Approved EU object-storage endpoint HOSTS for offsite backups (host / proper-subdomain
# match, NOT a loose suffix — "evil-fsn1.your-objectstorage.com" must NOT pass). A region
# label is not residency; assert_backup_endpoint_eu fails closed against this list. Operators
# add rows here (or extend via a future config key) when a new EU region is approved.
_EU_BACKUP_ENDPOINT_SUFFIXES = (
    "fsn1.your-objectstorage.com",   # Hetzner Falkenstein (DE)
    "nbg1.your-objectstorage.com",   # Hetzner Nuremberg (DE)
    "hel1.your-objectstorage.com",   # Hetzner Helsinki (FI)
)


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


def _is_https_url(value: str) -> bool:
    """Whether ``value`` is a safe HTTPS base URL for an internal control link.

    Query strings, fragments, and embedded credentials have no legitimate use in
    broker or fleet base URLs. Rejecting them here keeps a misconfigured control
    plane from leaking a credential or silently downgrading its transport.
    """
    try:
        parsed = urlsplit((value or "").strip())
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _is_readable_file(value: str) -> bool:
    """Check a TLS path without exposing its contents in an error or log."""
    path = Path((value or "").strip())
    if not value or not path.is_file():
        return False
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


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

    # AI Employees is provider-neutral, while the first live roster is Gemini.
    # Anthropic remains fail-closed until all three gates and its credential are present.
    ai_employees_max_output_tokens: int = 2048
    ai_employees_run_lease_seconds: int = 120
    ai_employees_run_heartbeat_seconds: int = 15
    ai_employees_provider_timeout_seconds: float = 60.0
    ai_employees_anthropic_enabled: bool = False
    ai_employees_anthropic_processing_approved: bool = False
    ai_employees_code_sandbox_enabled: bool = False
    ai_employees_google_client_id: str = ""
    ai_employees_google_client_secret: str = ""
    ai_employees_google_redirect_uri: str = ""
    ai_employees_google_timeout_seconds: float = 10.0
    ai_employees_connector_secret_store_path: str = ""

    # pgvector — only used when vector_store = "pgvector"
    database_url: str = ""
    migration_database_url: str = ""
    # Privileged DSN for cross-account operator/admin reads (list all accounts,
    # pending queues, dashboards). Must authenticate as a role that owns the
    # platform tables / bypasses RLS. Falls back to the migration then app DSN.
    operator_database_url: str = ""
    # A worker-only DSN for cross-tenant durable-job claims. It must authenticate
    # as the narrowly privileged worker login, not the app or owner/operator role.
    # Do not inject this secret into API containers.
    worker_database_url: str = ""

    # If set (comma-separated hostnames), a provisioning callback_url must be
    # https and its host must be in this allowlist. The workflow sends the
    # callback key as a bearer to this URL, so an unvalidated host is a secret-
    # exfiltration sink. Empty = no host restriction (dev/single-operator).
    provisioning_callback_allowed_hosts: str = ""
    secret_encryption_key: str = ""
    secret_encryption_key_version: str = "v1"
    bootstrap_secret_ttl_seconds: int = 3600
    # Explicit non-owner product logins. The OneBrain app/worker split is
    # enforced by 0030_job_queue_rls_roles; assistant and communication use
    # their own database-only credentials rendered by the fleet bootstrap.
    # `postgres_service_role` remains a legacy generic setting.
    postgres_app_role: str = ""
    postgres_worker_role: str = ""
    postgres_assistant_role: str = ""
    postgres_communication_role: str = ""
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
    # Customer boxes receive a non-secret topology descriptor plus app-specific
    # raw keys. Startup stores only the key hashes in the customer database.
    customer_bootstrap: str = ""
    assistant_service_key: str = ""
    communication_service_key: str = ""

    # Shared-password demo accounts (email + "onebrain2026") are convenient for a
    # local demo but must NEVER auto-seed on a real deployment. They seed only on a
    # fully-local stack unless this is explicitly opted into.
    allow_demo_users: bool = False

    # Cap on request body size (bytes) — a cheap DoS/edge guard. 50 MB leaves room
    # for large document/PDF uploads while rejecting absurd payloads.
    max_body_bytes: int = 50 * 1024 * 1024

    # Drive is an always-installed OneBrain Core capability. These settings
    # select replaceable storage/lifecycle limits; there is deliberately no
    # feature flag that removes the module from a customer deployment.
    # Safe rollout default for existing customer boxes that predate the explicit
    # Drive environment contract. Storage remains available; AI indexing must be
    # deliberately enabled after the deployment DPIA is signed.
    drive_policy_mode: str = "storage_only"  # disabled | storage_only | storage_and_indexing
    drive_private_spaces_enabled: bool = False  # Phase 2; ownership transfer needs revocation hooks first
    drive_data_dir: str = ""
    drive_max_file_bytes: int = 50 * 1024 * 1024
    drive_quota_bytes: int = 0
    drive_min_free_bytes: int = 512 * 1024 * 1024
    drive_min_free_percent: float = 5.0
    drive_upload_session_seconds: int = 24 * 60 * 60
    drive_max_folder_depth: int = 32

    # Mandatory Drive malware boundary. Local/test composition uses the
    # deterministic fake; production-like workers must use the native sandbox
    # launcher and cannot configure a disabled/clean-all adapter.
    drive_malware_scanner: str = "fake"  # fake (local/test) | clamav
    drive_malware_sandbox_binary: str = "/usr/local/bin/onebrain-scanner-sandbox"
    drive_malware_definition_baseline_dir: str = "/opt/onebrain/clamav-baseline"
    drive_malware_definition_runtime_dir: str = "/var/lib/onebrain/clamav"
    drive_malware_clamav_binary: str = "/usr/bin/clamscan"
    drive_malware_capabilities_file: str = "/opt/onebrain/scanner-capabilities.json"
    drive_malware_release_evidence_file: str = "/opt/onebrain/scanner-release.json"
    drive_malware_packages_file: str = "/opt/onebrain/scanner-packages.txt"
    drive_malware_scan_timeout_seconds: float = 60.0
    drive_malware_max_scan_time_ms: int = 45_000
    drive_malware_bytecode_timeout_ms: int = 5_000
    drive_malware_output_limit_bytes: int = 64 * 1024
    drive_malware_max_source_bytes: int = 50 * 1024 * 1024
    drive_malware_max_scan_bytes: int = 512 * 1024 * 1024
    drive_malware_max_file_bytes: int = 100 * 1024 * 1024
    drive_malware_max_archive_files: int = 10_000
    drive_malware_max_archive_recursion: int = 16
    drive_malware_definition_refresh_seconds: int = 6 * 60 * 60
    drive_malware_definition_max_age_seconds: int = 72 * 60 * 60
    drive_malware_definition_retain_inactive_sets: int = 2
    drive_malware_definition_prune_grace_seconds: int = 10 * 60
    drive_malware_retry_attempts: int = 5
    drive_malware_retry_cooldown_seconds: int = 15 * 60
    drive_malware_retry_max_cooldown_seconds: int = 6 * 60 * 60
    drive_malware_quarantine_bytes: int = 5 * 1024 * 1024 * 1024
    drive_malware_runtime_stale_seconds: int = 3 * 60
    drive_malware_worker_id: str = "worker_primary"

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
    # The secret domain-separates login, service-key, and fleet counters so
    # PostgreSQL-backed limits are shared by every API replica.
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900
    login_rate_limit_secret: str = ""
    trusted_proxy_cidrs: str = ""              # csv; headers ignored unless this is set
    trusted_proxy_hops: int = 0                 # trusted X-Forwarded-For hops including direct peer

    # --- Mission Control (fleet control plane) ---
    # operator_mode: this deployment IS Mission Control — it ingests fleet
    # heartbeats and serves the operator/fleet surface. operator_console:
    # whether the operator/fleet read surface is exposed at all (a
    # customer-serving deployment sets this false so its admins can't see fleet
    # state). A deployment reports to Mission Control when fleet_url + fleet_key
    # are set.
    operator_mode: bool = False
    operator_console: bool = False
    deployment_id: str = ""              # this deployment's control-plane id (for its heartbeat)
    fleet_url: str = ""                  # Mission Control base URL the reporter POSTs to
    fleet_key: str = ""                  # this deployment's fleet heartbeat key (fk_...)
    # Customer-facing Compose stacks set this false. Their root-only host agent
    # reports release-gate metadata without exposing fleet configuration inside
    # the application container.
    fleet_reporter_enabled: bool = True
    fleet_public_url: str = ""           # (Mission Control) its own public URL handed to enrolling deployments
    fleet_heartbeat_retention_days: int = 30   # prune fleet_heartbeats older than this
    fleet_report_seconds: int = 60       # how often the reporter posts a heartbeat
    fleet_missed_heartbeat_seconds: int = 600   # watchdog: alert when older than this
    fleet_target_version: str = ""       # expected fleet bundle version (drift alerting)
    # Fleet health watchdog. It only opens/resolves metadata-only alerts; unlike
    # rollout reconciliation it never changes a deployment, so it is enabled on
    # Mission Control by default. Set 0 to disable the scheduler explicitly.
    fleet_watchdog_seconds: int = Field(default=60, ge=0)
    fleet_low_root_disk_percent: int = Field(default=15, ge=0, le=100)
    fleet_low_data_disk_percent: int = Field(default=15, ge=0, le=100)
    # Persistent PostgreSQL/data mount as seen by a reporter. A container that
    # does not mount it emits 0/0 (unknown), never a duplicate root filesystem.
    fleet_data_volume_path: str = "/mnt/onebrain-data"
    # Heartbeat ingest guards (Mission Control side). Cap per-deployment posting
    # rate so a leaked/misused fleet key can't flood the append-only table, and
    # reject a reported_at that is implausibly skewed from server time (received_at
    # stays authoritative for the watchdog regardless).
    fleet_heartbeat_rate_limit: int = 120        # max heartbeats per window per deployment
    fleet_heartbeat_rate_window_seconds: int = 60
    fleet_heartbeat_max_skew_seconds: int = 3600 # reject reported_at farther than this from now

    # --- Release trust primitives (Hetzner P0; enforcement flags default OFF) ---
    release_verify_public_key: str = ""        # base64 raw Ed25519 public key; "" disables verification
    release_require_signature: bool = False    # reject unsigned release creation + block unsigned in plans
    release_require_signed_images: bool = False # require a non-empty digest-pinned images map on creation
    release_require_rollback_kind: bool = False # require rollback_kind in {code_only,restore_required} on creation
    release_registry_allowlist: str = "ghcr.io/proark1"  # csv of allowed image-ref PREFIXES host[/org[/repo]]
    release_promotion_required: bool = False  # hard customer gate; report-only warnings while false
    dev_release_verify_public_key: str = ""   # CI development-signing public key; never trusted by customers
    release_candidate_key_id: str = ""        # narrowly scoped CI candidate credential id
    release_candidate_key_hash: str = ""      # sha256$... hash; raw secret is never stored
    # ^ repo-prefix granular (B2): a bare host like "ghcr.io" would allowlist every GHCR tenant. The default
    #   is the org prefix the Phase-2 GHCR CI publishes under; override via ONEBRAIN_RELEASE_REGISTRY_ALLOWLIST
    #   if the CI org ever differs (a wrong default 400s the first images-carrying release — fail-closed, safe).

    # --- Ground-truth reporter ---
    build_version: str = ""                    # CI-stamped running version (ONEBRAIN_BUILD_VERSION); "" -> app.__version__
    module_probes_enabled: bool = False        # probe co-located module /health endpoints for the heartbeat
    local_modules: str = ""                    # csv of MODULE_IDS running on this box (customer compose sets it)

    # --- Hetzner provisioner (P1; dormant until provisioner_backend="hetzner") ---
    # Provisioning is deliberately OFF by default. Production Mission Control
    # may explicitly opt into only the dedicated Hetzner broker path; the legacy
    # The retired workflow dispatcher is not a supported backend anymore.
    provisioner_backend: Literal["disabled", "hetzner"] = "disabled"
    hetzner_api_token: str = ""                # broker-only secret; MC must leave this empty for remote provisioning
    hetzner_broker_url: str = ""               # HTTPS origin of the dedicated remote broker
    hetzner_broker_credential: str = ""        # MC-only broker bearer credential; never the Hetzner token
    hetzner_broker_client_certificate_file: str = ""  # MC mTLS client certificate path
    hetzner_broker_client_key_file: str = ""          # MC mTLS private-key path
    hetzner_broker_ca_file: str = ""                  # optional private CA bundle for broker TLS
    hetzner_broker_timeout_seconds: float = 10.0
    hetzner_allow_inprocess_broker: bool = False  # A6: dogfood/test escape hatch. Production Hetzner MUST use the
                                               # out-of-process broker (hetzner_broker_url). See build_hetzner_broker.
    hetzner_location: str = "nbg1"             # EU region (nbg1/fsn1/hel1)
    hetzner_server_type: str = "cx23"          # current cheapest CX (cx22 is no longer offered)
    hetzner_image: str = "ubuntu-24.04"
    hetzner_ssh_key_ids: str = ""              # csv of Hetzner SSH key ids (break-glass only; no inbound 22 path)
    hetzner_volume_size_gb: int = 10           # data volume so Postgres survives a rebuild
    hetzner_firewall_id: str = ""              # pre-created default-deny firewall, attached in the create call
    # COST CIRCUIT BREAKER (ONEBRAIN_HETZNER_MAX_FLEET_SERVERS): the broker refuses to create
    # a new box once this many `managed-by=onebrain-fleet` servers already exist (the
    # idempotency reuse of an existing deployment never counts against it). Nothing else in
    # the fleet caps server creation, so a retry/loop/replay bug could otherwise mint many
    # billable boxes. Raise it to grow the fleet; <=0 disables the breaker.
    hetzner_max_fleet_servers: int = 5
    hetzner_enable_backups: bool = True   # ONEBRAIN_HETZNER_ENABLE_BACKUPS: after create_server the broker
                                          # calls POST /servers/{id}/actions/enable_backup. NOTE: Hetzner
                                          # Backups image the ROOT DISK ONLY (not the /mnt/onebrain-data
                                          # volume holding Postgres) — convenience whole-box DR, NOT data DR.
                                          # The authoritative DB DR is the offsite pg_dump path (ONEBRAIN_BACKUP_*).
    # --- DNS (P1) ---
    # DNS rides the UNIFIED Hetzner Cloud API (GA 2025-11-10): the SAME hetzner_api_token
    # (Bearer) authenticates DNS — there is NO separate DNS token. The legacy
    # dns.hetzner.com + Auth-API-Token path (and its own token) is gone.
    fleet_dns_provider: str = ""               # "" | hetzner | cloudflare  ("" -> skip DNS, use raw IP)
    fleet_dns_zone_id: str = ""                # Hetzner Cloud DNS zone id OR name (the rrset path accepts either)
    fleet_base_domain: str = ""                # e.g. "fleet.example" -> <deployment_id>.fleet.example
    # --- Desired-state emission (P2; MC side). The ONE online private key MC holds (D-11). ---
    fleet_desired_state_private_key: str = ""  # base64 raw Ed25519 wrapper key; "" DISABLES emission (dormant)
    fleet_desired_state_public_key: str = ""   # baked into cloud-init so the box can verify the wrapper
    fleet_desired_state_ttl_seconds: int = 900 # envelope expiry window
    # --- Pull reconcile (P2) ---
    fleet_pull_convergence_deadline_seconds: int = 1800  # a box silent past this after an offer -> synth failed

    # --- Phase 5: desired-state wrapper-key rotation (P5-02) ---
    fleet_desired_state_public_keys: str = ""   # csv of ACCEPTED wrapper public keys delivered to boxes
                                                # (rotation overlap set). "" -> falls back to the singular
                                                # fleet_desired_state_public_key. MC still signs with the ONE
                                                # fleet_desired_state_private_key; boxes accept ANY key in this set.
    # --- Phase 5: bootstrap-token exchange (P5-03) ---
    fleet_bootstrap_token_ttl_seconds: int = 3600   # first-boot token validity window
    # G1-5: the bootstrap exchange returns the FULL secret bundle, so a single fetch
    # exfiltrates everything a leaked fleet key could reach. Cap it FAR below the
    # 120/60s heartbeat budget (a handful per minute) with a DEDICATED limiter so a
    # leaked key cannot poll the bundle. Keyed per deployment (bootstrap:<deployment_id>).
    fleet_bootstrap_rate_limit: int = 5
    fleet_bootstrap_rate_window_seconds: int = 60
    # --- Phase 5: reconcile scheduler (P5-04) ---
    fleet_reconcile_seconds: int = 0            # 0 = DISABLED (default). >0 = in-process pull-reconcile
                                                # tick interval in seconds (MC only), clamped to a 30s floor.
                                                # (G3-4) Default 0 keeps the daemon OFF on the already-deployed
                                                # dormant MC — auto-advance is opt-in, never flipped by a deploy.
    # --- Phase 5: network boundary (P5-05) ---
    hetzner_firewall_allow_ssh: bool = False    # break-glass ONLY; default false = NO inbound 22 rule emitted

    # --- Fleet offsite backups (P4-G / Part 2). OFF by default -> the whole feature is INERT
    #     until a bucket is configured; the box agent (onebrain_backup.sh) exits 0 immediately
    #     when backup_enabled is false. Hetzner server Backups (above) cover the ROOT DISK; THIS
    #     is the authoritative DATABASE DR (offsite encrypted pg_dump to EU Object Storage). ---
    backup_enabled: bool = False                 # ONEBRAIN_BACKUP_ENABLED — master gate
    backup_object_store_endpoint: str = ""       # ONEBRAIN_BACKUP_S3_ENDPOINT e.g. https://fsn1.your-objectstorage.com
    backup_object_store_bucket: str = ""         # ONEBRAIN_BACKUP_S3_BUCKET (EU-region bucket)
    backup_object_store_region: str = ""         # ONEBRAIN_BACKUP_S3_REGION SigV4 region label e.g. "fsn1"
    backup_object_store_access_key: str = ""     # ONEBRAIN_BACKUP_S3_ACCESS_KEY (SECRET — delivered via sealed bundle)
    backup_object_store_secret_key: str = ""     # ONEBRAIN_BACKUP_S3_SECRET_KEY (SECRET — delivered via sealed bundle)
    backup_schedule: str = "daily"               # ONEBRAIN_BACKUP_SCHEDULE — systemd OnCalendar baked into the timer
    backup_retention_days: int = 30              # ONEBRAIN_BACKUP_RETENTION_DAYS — the S3 lifecycle-expiration rule
                                                 # (OPERATOR SETUP) is the AUTHORITATIVE retention backstop; the box
                                                 # prune is only an optimization. Boxes are PUT/GET/LIST only (no DELETE).

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
    def is_operator_surface(self) -> bool:
        """True when this deployment is allowed to expose the operator control
        plane — Mission Control (operator_mode) or a stack explicitly opting into
        the local operator console. A pure customer-serving stack sets both false
        so the operator + provisioning surface is not mounted or served at all."""
        return bool(self.operator_mode or self.operator_console)

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

    def assert_drive_malware_scanner_configured(self) -> None:
        """Validate the replaceable scanner without weakening the boundary.

        This check is intentionally callable by worker composition/preflight,
        rather than at ``Settings`` construction, so the API image can remain
        independent from native scanner files. The scanner factory calls it as
        well, making an unknown or production fake adapter fail closed.
        """

        adapter = self.drive_malware_scanner.strip().lower()
        errors: list[str] = []
        if adapter not in {"fake", "clamav"}:
            errors.append("ONEBRAIN_DRIVE_MALWARE_SCANNER must be clamav (or fake for local tests)")
        if self.is_production_like and adapter != "clamav":
            errors.append("production-like deployments require ONEBRAIN_DRIVE_MALWARE_SCANNER=clamav")

        positive_values = (
            ("ONEBRAIN_DRIVE_MALWARE_SCAN_TIMEOUT_SECONDS", self.drive_malware_scan_timeout_seconds),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_SCAN_TIME_MS", self.drive_malware_max_scan_time_ms),
            ("ONEBRAIN_DRIVE_MALWARE_BYTECODE_TIMEOUT_MS", self.drive_malware_bytecode_timeout_ms),
            ("ONEBRAIN_DRIVE_MALWARE_OUTPUT_LIMIT_BYTES", self.drive_malware_output_limit_bytes),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_SOURCE_BYTES", self.drive_malware_max_source_bytes),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_SCAN_BYTES", self.drive_malware_max_scan_bytes),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_FILE_BYTES", self.drive_malware_max_file_bytes),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_ARCHIVE_FILES", self.drive_malware_max_archive_files),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_ARCHIVE_RECURSION", self.drive_malware_max_archive_recursion),
            ("ONEBRAIN_DRIVE_MALWARE_DEFINITION_REFRESH_SECONDS", self.drive_malware_definition_refresh_seconds),
            ("ONEBRAIN_DRIVE_MALWARE_DEFINITION_MAX_AGE_SECONDS", self.drive_malware_definition_max_age_seconds),
            (
                "ONEBRAIN_DRIVE_MALWARE_DEFINITION_RETAIN_INACTIVE_SETS",
                self.drive_malware_definition_retain_inactive_sets,
            ),
            (
                "ONEBRAIN_DRIVE_MALWARE_DEFINITION_PRUNE_GRACE_SECONDS",
                self.drive_malware_definition_prune_grace_seconds,
            ),
            ("ONEBRAIN_DRIVE_MALWARE_RETRY_ATTEMPTS", self.drive_malware_retry_attempts),
            ("ONEBRAIN_DRIVE_MALWARE_RETRY_COOLDOWN_SECONDS", self.drive_malware_retry_cooldown_seconds),
            ("ONEBRAIN_DRIVE_MALWARE_RETRY_MAX_COOLDOWN_SECONDS", self.drive_malware_retry_max_cooldown_seconds),
            ("ONEBRAIN_DRIVE_MALWARE_QUARANTINE_BYTES", self.drive_malware_quarantine_bytes),
            ("ONEBRAIN_DRIVE_MALWARE_RUNTIME_STALE_SECONDS", self.drive_malware_runtime_stale_seconds),
        )
        errors.extend(f"{name} must be positive" for name, value in positive_values if value <= 0)
        if self.drive_malware_max_source_bytes < self.drive_max_file_bytes:
            errors.append("ONEBRAIN_DRIVE_MALWARE_MAX_SOURCE_BYTES must cover DRIVE_MAX_FILE_BYTES")
        if self.drive_malware_max_source_bytes > self.drive_malware_max_file_bytes:
            errors.append("ONEBRAIN_DRIVE_MALWARE_MAX_SOURCE_BYTES cannot exceed MAX_FILE_BYTES")
        if self.drive_malware_max_file_bytes > self.drive_malware_max_scan_bytes:
            errors.append("ONEBRAIN_DRIVE_MALWARE_MAX_FILE_BYTES cannot exceed MAX_SCAN_BYTES")
        upper_bounds = (
            ("ONEBRAIN_DRIVE_MALWARE_OUTPUT_LIMIT_BYTES", self.drive_malware_output_limit_bytes, 1024 * 1024),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_SCAN_BYTES", self.drive_malware_max_scan_bytes, 512 * 1024 * 1024),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_FILE_BYTES", self.drive_malware_max_file_bytes, 100 * 1024 * 1024),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_ARCHIVE_FILES", self.drive_malware_max_archive_files, 10_000),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_ARCHIVE_RECURSION", self.drive_malware_max_archive_recursion, 16),
            ("ONEBRAIN_DRIVE_MALWARE_SCAN_TIMEOUT_SECONDS", self.drive_malware_scan_timeout_seconds, 5 * 60),
            ("ONEBRAIN_DRIVE_MALWARE_MAX_SCAN_TIME_MS", self.drive_malware_max_scan_time_ms, 90 * 1000),
            ("ONEBRAIN_DRIVE_MALWARE_BYTECODE_TIMEOUT_MS", self.drive_malware_bytecode_timeout_ms, 30 * 1000),
            ("ONEBRAIN_DRIVE_MALWARE_RETRY_ATTEMPTS", self.drive_malware_retry_attempts, 10),
            (
                "ONEBRAIN_DRIVE_MALWARE_DEFINITION_RETAIN_INACTIVE_SETS",
                self.drive_malware_definition_retain_inactive_sets,
                10,
            ),
        )
        errors.extend(
            f"{name} exceeds the packaged scanner sandbox ceiling"
            for name, value, maximum in upper_bounds
            if value > maximum
        )
        if self.drive_malware_retry_max_cooldown_seconds < self.drive_malware_retry_cooldown_seconds:
            errors.append("ONEBRAIN_DRIVE_MALWARE_RETRY_MAX_COOLDOWN_SECONDS cannot be below the initial cooldown")
        if self.drive_malware_definition_refresh_seconds > self.drive_malware_definition_max_age_seconds:
            errors.append("definition refresh interval cannot exceed the maximum definition age")
        if self.drive_malware_max_scan_time_ms >= int(self.drive_malware_scan_timeout_seconds * 1000):
            errors.append("ClamAV max scan time must be below the outer process timeout")
        if self.drive_malware_bytecode_timeout_ms > self.drive_malware_max_scan_time_ms:
            errors.append("ClamAV bytecode timeout cannot exceed max scan time")
        if self.drive_malware_definition_prune_grace_seconds <= self.drive_malware_scan_timeout_seconds:
            errors.append("definition prune grace must exceed the outer scanner timeout")
        if self.is_production_like:
            for name, value in (
                ("ONEBRAIN_DRIVE_MALWARE_SANDBOX_BINARY", self.drive_malware_sandbox_binary),
                ("ONEBRAIN_DRIVE_MALWARE_DEFINITION_BASELINE_DIR", self.drive_malware_definition_baseline_dir),
                ("ONEBRAIN_DRIVE_MALWARE_DEFINITION_RUNTIME_DIR", self.drive_malware_definition_runtime_dir),
                ("ONEBRAIN_DRIVE_MALWARE_CLAMAV_BINARY", self.drive_malware_clamav_binary),
                ("ONEBRAIN_DRIVE_MALWARE_CAPABILITIES_FILE", self.drive_malware_capabilities_file),
                ("ONEBRAIN_DRIVE_MALWARE_RELEASE_EVIDENCE_FILE", self.drive_malware_release_evidence_file),
                ("ONEBRAIN_DRIVE_MALWARE_PACKAGES_FILE", self.drive_malware_packages_file),
            ):
                if not PurePosixPath(value.strip()).is_absolute():
                    errors.append(f"{name} must be an absolute path in production")
        if errors:
            raise RuntimeError("Drive malware scanner configuration is unsafe: " + "; ".join(errors))

    def assert_production_mission_control_ready(self) -> None:
        """Fail closed when a production-like Mission Control is incomplete.

        Local development and customer data-plane deployments retain their
        lightweight defaults. Any production-like operator surface (including
        ``operator_console``) can invoke write-capable deployment endpoints, so
        it must have the fully isolated Hetzner/mTLS control path and every
        release, RLS, and reconcile guard armed before it serves traffic.
        """
        if not (self.is_production_like and self.is_operator_surface):
            return

        errors: list[str] = []

        if self.provisioner_backend != "hetzner":
            errors.append("set ONEBRAIN_PROVISIONER_BACKEND=hetzner")
        if self.hetzner_allow_inprocess_broker:
            errors.append("ONEBRAIN_HETZNER_ALLOW_INPROCESS_BROKER must be false (in-process broker is forbidden)")
        if self.hetzner_api_token.strip():
            errors.append("ONEBRAIN_HETZNER_API_TOKEN must be empty on Mission Control")
        if not _is_https_url(self.hetzner_broker_url):
            errors.append("set ONEBRAIN_HETZNER_BROKER_URL to an HTTPS broker URL")
        if not self.hetzner_broker_credential.strip():
            errors.append("set ONEBRAIN_HETZNER_BROKER_CREDENTIAL")

        for name, value in (
            ("ONEBRAIN_HETZNER_BROKER_CLIENT_CERTIFICATE_FILE", self.hetzner_broker_client_certificate_file),
            ("ONEBRAIN_HETZNER_BROKER_CLIENT_KEY_FILE", self.hetzner_broker_client_key_file),
        ):
            if not _is_readable_file(value):
                errors.append(f"set {name} to a readable mTLS file")
        if self.hetzner_broker_ca_file.strip() and not _is_readable_file(self.hetzner_broker_ca_file):
            errors.append("set ONEBRAIN_HETZNER_BROKER_CA_FILE to a readable CA file")

        for name, value in (
            ("ONEBRAIN_FLEET_URL", self.fleet_url),
            ("ONEBRAIN_FLEET_PUBLIC_URL", self.fleet_public_url),
        ):
            if not _is_https_url(value):
                errors.append(f"set {name} to an HTTPS URL")
        if not self.fleet_key.strip():
            errors.append("set ONEBRAIN_FLEET_KEY")
        if not self.deployment_id.strip():
            errors.append("set ONEBRAIN_DEPLOYMENT_ID")

        if not self.fleet_desired_state_private_key.strip():
            errors.append("set ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY")
        if not (self.fleet_desired_state_public_keys.strip() or self.fleet_desired_state_public_key.strip()):
            errors.append("set ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS (or the singular public key)")
        elif self.fleet_desired_state_private_key.strip():
            try:
                from app.controlplane.desired_state import active_signer_in_served_set

                if not active_signer_in_served_set(self):
                    errors.append("the active desired-state signer must be in ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS")
            except Exception:
                errors.append("ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY must be a valid signing key")
        if int(self.fleet_desired_state_ttl_seconds) <= 0:
            errors.append("ONEBRAIN_FLEET_DESIRED_STATE_TTL_SECONDS must be positive")

        if not self.release_verify_public_key.strip():
            errors.append("set ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY")
        if not self.release_registry_allowlist.strip():
            errors.append("set ONEBRAIN_RELEASE_REGISTRY_ALLOWLIST")
        for attribute, name in (
            ("release_require_signature", "ONEBRAIN_RELEASE_REQUIRE_SIGNATURE=true"),
            ("release_require_signed_images", "ONEBRAIN_RELEASE_REQUIRE_SIGNED_IMAGES=true"),
            ("release_require_rollback_kind", "ONEBRAIN_RELEASE_REQUIRE_ROLLBACK_KIND=true"),
            ("release_promotion_required", "ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true"),
        ):
            if not getattr(self, attribute):
                errors.append(f"set {name}")

        if self.vector_store != "pgvector":
            errors.append("set ONEBRAIN_VECTOR_STORE=pgvector")
        if not self.database_url.strip():
            errors.append("set ONEBRAIN_DATABASE_URL")
        if not self.operator_database_url.strip():
            errors.append("set ONEBRAIN_OPERATOR_DATABASE_URL")
        elif self.database_url.strip():
            app_database_role = (urlsplit(self.database_url.strip()).username or "").lower()
            operator_database_role = (
                urlsplit(self.operator_database_url.strip()).username or ""
            ).lower()
            if not app_database_role:
                errors.append("ONEBRAIN_DATABASE_URL must include a PostgreSQL login role")
            if not operator_database_role:
                errors.append("ONEBRAIN_OPERATOR_DATABASE_URL must include a PostgreSQL login role")
            elif operator_database_role == app_database_role:
                errors.append(
                    "ONEBRAIN_OPERATOR_DATABASE_URL must use a distinct PostgreSQL login role"
                )
        if not self.rls_enforced:
            errors.append("set ONEBRAIN_RLS_ENFORCED=true")
        try:
            # Do not duplicate the provisioning cipher's parsing rules here. An
            # invalid key must fail before startup or a write-capable provisioning
            # endpoint can create a bundle encrypted with an unusable key.
            from app.provisioning.runs import OneTimeSecretCipher

            OneTimeSecretCipher(self, require_encoded_key=True)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
        try:
            from app.db.rls import PostgresRoleError, validate_job_role_configuration

            validate_job_role_configuration(self)
        except PostgresRoleError as exc:
            errors.append(str(exc))
        if len(self.login_rate_limit_secret) < 32:
            errors.append("set ONEBRAIN_LOGIN_RATE_LIMIT_SECRET to a distinct 32+ character secret")
        if not any(host.strip() for host in self.provisioning_callback_allowed_hosts.split(",")):
            errors.append("set ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS to the approved callback hostnames")
        if self.trusted_proxy_hops < 0:
            errors.append("ONEBRAIN_TRUSTED_PROXY_HOPS cannot be negative")
        if self.trusted_proxy_hops and not self.trusted_proxy_cidrs.strip():
            errors.append("set ONEBRAIN_TRUSTED_PROXY_CIDRS when ONEBRAIN_TRUSTED_PROXY_HOPS is enabled")
        if int(self.fleet_reconcile_seconds) <= 0:
            errors.append("set ONEBRAIN_FLEET_RECONCILE_SECONDS to a positive interval")

        if errors:
            raise RuntimeError("Production Mission Control configuration is incomplete: " + "; ".join(errors))

    @property
    def backup_object_store_configured(self) -> bool:
        """True when an offsite backup target is FULLY specified (endpoint + bucket + both creds)."""
        return bool(self.backup_object_store_endpoint and self.backup_object_store_bucket
                    and self.backup_object_store_access_key and self.backup_object_store_secret_key)

    def assert_backup_endpoint_eu(self) -> None:
        """Fail closed if backups are ENABLED but the endpoint host is not an approved EU host.
        A region LABEL is not residency; this ASSERTS storage-residency (GDPR) before any offsite
        write. build_object_store (BK4) calls it before constructing the real store, so a typo or a
        copy-pasted non-EU endpoint refuses rather than silently exfiltrating EU personal data."""
        if not self.backup_enabled:
            return
        host = (urlsplit(self.backup_object_store_endpoint).hostname or "").lower()
        if not any(host == s or host.endswith("." + s) for s in _EU_BACKUP_ENDPOINT_SUFFIXES):
            raise ValueError(
                f"ONEBRAIN_BACKUP_S3_ENDPOINT host {host!r} is not an approved EU endpoint; "
                "refusing to back up EU personal data offshore")

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

    @property
    def pg_worker_database_url(self) -> str:
        """Worker-only durable-job DSN, with no privileged fallback.

        An API process intentionally receives an empty value, so calling a
        worker-only job method there fails rather than silently borrowing the
        owner or app role.  The deployment worker validates the non-empty DSN
        before it begins claiming work.
        """
        dsn = self.worker_database_url.strip()
        if dsn:
            _guard_pytest_dsn(dsn)
        return dsn


@lru_cache
def get_settings() -> Settings:
    return Settings()

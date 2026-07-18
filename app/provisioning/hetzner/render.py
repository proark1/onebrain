"""Pure render layer (P4-02): deterministic text generation for one box from
(deployment, enabled modules, release manifest images, secret *references*).

Pure functions, golden-file tested, dependency-free (no PyYAML/Jinja — strings are
assembled directly). Ports come from `app.module_manifest.MODULE_HEALTH_PROBES`
(H-4), never a legacy :8080 port mask. One compose file for all products, gated by PROFILES
keyed to the enabled modules (H-5). One-shot migrate services gate the long-running
services (H-6). Per-product databases on one Postgres (A13). Per-service env files
(no inline secrets). The renderer ONLY ever emits `${VAR}` references for secrets, so
a golden file never contains plaintext — the metadata-endpoint egress block (A5/A10)
bounds their exposure until the Phase-5 bootstrap-token exchange fills them.

Injection discipline: `deployment_id`/`compose_project`/`fqdn` pass a strict charset
check and `images` values pass `validate_image_ref`; anything failing raises
`ValueError` (never emits)."""

from __future__ import annotations

import ast
import base64
import gzip
import io
import json
import lzma
import re
import tarfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from app.controlplane.base import MODULE_IDS, validate_image_ref
from app.module_manifest import MODULE_ENV_REQUIREMENTS, MODULE_HEALTH_PROBES
from app.provisioning.customer_bootstrap import decode_customer_bootstrap

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_BOX = _REPO_ROOT / "deploy" / "box"
_DEPLOY_TEMPLATES = _REPO_ROOT / "deploy" / "templates"

# operator-config/server-minted id charset (injection guard).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Short-lived token charset (P5-03). The bootstrap + callback tokens are baked into
# box.env and flow into a shell/URL sink; secrets.token_urlsafe(...) and the
# bt_<id>_<secret> grammar both stay within this set.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PG_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")

# Canonical module order (golden determinism).
MODULE_ORDER = (
    "onebrain-api",
    "onebrain-admin-ui",
    "onebrain-workers",
    "assistant-service",
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
)
PRODUCTS = ("onebrain", "assistant", "communication")
# module_id -> the product profile it belongs to.
_PRODUCT_OF = {m: ("onebrain" if m.startswith("onebrain") else m.split("-", 1)[0]) for m in MODULE_ORDER}
# product -> (migrate service base module image, migrate command, db name).
_MIGRATE = {
    # Drive malware revision 0033 is intentionally schema-only.  The one-shot
    # maintenance container must run Alembic and the bounded activation before
    # Compose is allowed to start any long-running OneBrain process.
    "onebrain": (
        "onebrain-api",
        ["python", "-m", "app.drive.malware.activation", "--migrate"],
        "onebrain",
    ),
    "communication": ("communication-api", ["pnpm", "db:migrate"], "communication"),
    "assistant": ("assistant-service", ["alembic", "upgrade", "head"], "assistant"),
}
_DB_OF = {"onebrain": "onebrain", "assistant": "assistant", "communication": "communication"}
_PG_USER = "onebrain"
_DEFAULT_APP_ROLE = "onebrain_app"
_DEFAULT_WORKER_ROLE = "onebrain_worker"
_DEFAULT_ASSISTANT_ROLE = "assistant_app"
_DEFAULT_COMMUNICATION_ROLE = "communication_app"
# Support images are part of the trusted release surface too: do not let a
# later pull of a mutable infrastructure tag change a provisioned box.
_CADDY_IMAGE = "caddy:2@sha256:844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9"
_PGVECTOR_IMAGE = "pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
_REDIS_IMAGE = "redis:7@sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954"
# The API trusts forwarded client addresses only from Caddy on this small,
# private network. The static address prevents a default-network container from
# spoofing the trusted proxy when the API is scaled horizontally.
_EDGE_NETWORK = "edge"
_EDGE_SUBNET = "172.30.0.0/24"
_CADDY_EDGE_IP = "172.30.0.2"
_API_EDGE_ALIAS = "api-edge"
# The MC API runs as uid 10001. Its broker client key is written outside the
# general asset archive and bind-mounted read-only only into that container.
_OPERATOR_TLS_HOST_DIR = "/opt/onebrain/broker-tls"
_OPERATOR_TLS_CONTAINER_DIR = "/run/onebrain/broker-tls"
_BOOTSTRAP_ASSET_B85 = "/root/ob.b85"
_MALWARE_DEFINITION_CACHE_DIR = "/var/lib/onebrain/clamav"
_MALWARE_DEFINITION_TMPFILES = (
    f"d {_MALWARE_DEFINITION_CACHE_DIR} 0700 10001 10001 -\n"
)
# Assad Dar AI Communication deliberately ships one immutable image that starts a
# particular service according to SERVICE. Keep the release manifest module IDs
# separate for health, version, and rollout accounting, while selecting the
# correct process inside the shared image on a customer host.
_COMMUNICATION_SERVICE_SELECTORS = {
    "communication-api": "api",
    "communication-widget": "widget",
    "communication-voice": "voice",
    "communication-workers": "workers",
}


@dataclass(frozen=True)
class SecretRefs:
    """References the box resolves at boot, NOT plaintext. In P4 these are env-file
    PLACEHOLDERS the bootstrap-token exchange (P1-E, OUT) fills; the RENDERER only
    ever emits ${VAR} refs so a golden file never contains a secret."""

    fleet_key_env: str = "ONEBRAIN_FLEET_KEY"
    llm_key_env: str = "ONEBRAIN_LLM_API_KEY"
    db_password_env: str = "POSTGRES_PASSWORD"
    app_db_password_env: str = "POSTGRES_APP_PASSWORD"
    worker_db_password_env: str = "POSTGRES_WORKER_PASSWORD"
    assistant_db_password_env: str = "POSTGRES_ASSISTANT_PASSWORD"
    communication_db_password_env: str = "POSTGRES_COMMUNICATION_PASSWORD"
    redis_password_env: str = "REDIS_PASSWORD"
    auth_secret_env: str = "ONEBRAIN_AUTH_SECRET"   # session-cookie signing secret; app/main.py refuses to boot without a strong one
    login_rate_limit_secret_env: str = "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"
    # Mission Control-only secrets. They deliberately stay out of the shared
    # customer bootstrap-bundle contract: a customer box must never receive a
    # broker credential or the MC's bundle-encryption key.
    broker_credential_env: str = "ONEBRAIN_HETZNER_BROKER_CREDENTIAL"
    secret_encryption_key_env: str = "ONEBRAIN_SECRET_ENCRYPTION_KEY"
    owner_bootstrap_env: str = "ONEBRAIN_ADMIN_PASSWORD"
    admin_email_env: str = "ONEBRAIN_ADMIN_EMAIL"   # paired with owner_bootstrap_env; seed.py needs BOTH to seed a loginable admin
    service_key_env: str = "ONEBRAIN_SERVICE_KEY"
    space_id_env: str = "ONEBRAIN_SPACE_ID"
    assistant_service_key_env: str = "ONEBRAIN_ASSISTANT_SERVICE_KEY"
    communication_service_key_env: str = "ONEBRAIN_COMMUNICATION_SERVICE_KEY"
    communication_space_id_env: str = "ONEBRAIN_COMMUNICATION_SPACE_ID"
    backup_key_env: str = "UPDATE_BACKUP_KEY"   # A5: per-box client-side backup key; lives in box.env.
    # BK3: offsite-backup S3 credentials — SECRETS, so ${VAR} refs filled from the exchanged .env.
    backup_s3_access_key_env: str = "ONEBRAIN_BACKUP_S3_ACCESS_KEY"
    backup_s3_secret_key_env: str = "ONEBRAIN_BACKUP_S3_SECRET_KEY"


@dataclass(frozen=True)
class BoxRenderInputs:
    deployment_id: str
    account_id: str
    compose_project: str                 # "onebrain-<deployment_id>" (D-6 railway_environment_id)
    enabled_modules: tuple               # subset of MODULE_IDS (from DeploymentModule rows)
    images: dict                         # module_id -> registry/repo@sha256:...  (ReleaseManifest.images)
    fqdn: str = ""                       # <deployment_id>.<fleet_base_domain> ("" -> serve on IP, http only)
    fleet_url: str = ""                  # MC base URL (heartbeat + desired-state GET)
    run_id: str = ""                     # provisioning run id baked into the box's callback URL + box.env
                                         # (required to render cloud-init; the box POSTs its smoke result +
                                         # bootstrap_password to /api/provisioning/runs/<run_id>/callback)
    callback_url: str = ""               # validated per-run HTTPS template, with a literal {run_id}
    fleet_public_desired_state_key: str = ""   # baked so the box verifies the wrapper (H-7)
    release_public_key: str = ""               # baked so the box verifies the offline release sig
    release_version: str = ""                  # provision-time metadata for the root-only reporter
    release_migration: str = ""                # expected initial migration (metadata only)
    module_versions: dict = field(default_factory=dict)
    registry_allowlist: str = ""               # baked box-local allowlist (B2) — never envelope-supplied
    role: str = "customer"                      # A14: "customer" | "operator" (operator overlay is dormant in P4)
    bootstrap_token: str = ""                   # P5-03: the single-use first-boot token, baked in box.env; the box
                                                # exchanges it ONCE for /opt/onebrain/.env. Empty for the MC box
                                                # (role=operator, G3-1: baked .env, no exchange) + pure-executor tests.
    callback_token: str = ""                    # G1-7: the provisioning callback bearer, BAKED in box.env (not a
                                                # ${VAR} ref) so the metadata-egress-block FAILURE callback
                                                # authenticates BEFORE the bundle exchange runs.
    dotenv: str = ""                            # P5-06 (G3-1): the MC box (role=operator) BAKES its full
                                                # /opt/onebrain/.env into its MC-only secret archive — it boots with an
                                                # EMPTY DB and cannot self-exchange (a self-served /bootstrap 404s,
                                                # so its own Postgres would never come up). This is the
                                                # render_dotenv(mc_bundle) body (+ the operator-overlay ${VAR}
                                                # values). Empty for a CUSTOMER box, which FETCHES /opt/onebrain/.env
                                                # via the exchange (onebrain_bootstrap.sh) instead.
    customer_bootstrap: str = ""                 # Non-secret, versioned customer topology descriptor.
    postgres_app_role: str = _DEFAULT_APP_ROLE
    postgres_worker_role: str = _DEFAULT_WORKER_ROLE
    postgres_assistant_role: str = _DEFAULT_ASSISTANT_ROLE
    postgres_communication_role: str = _DEFAULT_COMMUNICATION_ROLE
    # MC-only remote broker configuration. The credential itself remains a
    # dotenv reference; PEM material is emitted as root-owned cloud-init assets
    # and mounted read-only only into the API container.
    operator_broker_url: str = ""
    operator_broker_client_certificate: str = ""
    operator_broker_client_key: str = ""
    operator_broker_ca: str = ""
    operator_fleet_reconcile_seconds: int = 0
    secret_refs: SecretRefs = field(default_factory=SecretRefs)
    # BK3: non-secret offsite-backup config, baked into box.env at render time (the two S3
    # credentials are secrets and ride the exchanged .env as ${VAR} refs, not these). All
    # default to the OFF/empty state so a box renders inert until a bucket is configured.
    backup_enabled: bool = False
    backup_s3_endpoint: str = ""
    backup_s3_bucket: str = ""
    backup_s3_region: str = ""
    backup_retention_days: int = 30
    backup_dbs: tuple = ()                       # the enabled products' Postgres DB names (pg_dump targets)
    # Drive is always present on customer boxes. Policy is an explicit privacy
    # state, not an installation flag; new/synthetic deployments default to
    # durable organization with AI indexing dark until the DPIA is signed.
    drive_policy_mode: str = "storage_only"
    pii_phase: str = "synthetic"


# --- validation --------------------------------------------------------------
def _validate(inp: BoxRenderInputs) -> None:
    for label, value in (("deployment_id", inp.deployment_id), ("compose_project", inp.compose_project)):
        if not _ID_RE.match(value or ""):
            raise ValueError(f"invalid {label} (charset ^[a-z0-9][a-z0-9._-]*$): {value!r}")
    if inp.drive_policy_mode not in {"disabled", "storage_only", "storage_and_indexing"}:
        raise ValueError("invalid Drive policy mode")
    if inp.pii_phase not in {"synthetic", "dpia_signed"}:
        raise ValueError("invalid PII phase")
    if inp.drive_policy_mode == "storage_and_indexing" and inp.pii_phase != "dpia_signed":
        raise ValueError("Drive indexing requires a signed DPIA")
    if inp.fqdn and not _ID_RE.match(inp.fqdn):
        raise ValueError(f"invalid fqdn (charset ^[a-z0-9][a-z0-9._-]*$): {inp.fqdn!r}")
    # run_id flows into the cloud-init callback URL (a shell sink) + box.env, so it is
    # held to the same charset guard when present (render_cloud_init also requires it).
    if inp.run_id and not _ID_RE.match(inp.run_id):
        raise ValueError(f"invalid run_id (charset ^[a-z0-9][a-z0-9._-]*$): {inp.run_id!r}")
    if inp.callback_url:
        _validate_callback_url_template(inp.callback_url)
    for label, role in (
        ("postgres_app_role", inp.postgres_app_role),
        ("postgres_worker_role", inp.postgres_worker_role),
        ("postgres_assistant_role", inp.postgres_assistant_role),
        ("postgres_communication_role", inp.postgres_communication_role),
    ):
        if not _PG_ROLE_RE.fullmatch(role or ""):
            raise ValueError(f"invalid {label}: expected a simple PostgreSQL login role name")
    runtime_roles = (
        inp.postgres_app_role,
        inp.postgres_worker_role,
        inp.postgres_assistant_role,
        inp.postgres_communication_role,
    )
    if len(set(runtime_roles)) != len(runtime_roles):
        raise ValueError("PostgreSQL runtime roles must all differ")
    if _PG_USER in runtime_roles:
        raise ValueError("PostgreSQL runtime roles must not use the owner login")
    if inp.customer_bootstrap:
        if inp.role != "customer":
            raise ValueError("customer_bootstrap is only valid for customer boxes")
        descriptor = decode_customer_bootstrap(inp.customer_bootstrap)
        if descriptor is None or descriptor.account_id != inp.account_id:
            raise ValueError("customer_bootstrap account does not match the rendered box")
    if inp.role != "operator" and any((
        inp.operator_broker_url,
        inp.operator_broker_client_certificate,
        inp.operator_broker_client_key,
        inp.operator_broker_ca,
    )):
        raise ValueError("operator broker configuration is only valid for operator boxes")
    if inp.role == "operator":
        has_certificate = bool(inp.operator_broker_client_certificate)
        has_key = bool(inp.operator_broker_client_key)
        if has_certificate != has_key:
            raise ValueError("operator broker certificate and key must be supplied together")
        for label, value in (
            ("operator_broker_client_certificate", inp.operator_broker_client_certificate),
            ("operator_broker_client_key", inp.operator_broker_client_key),
            ("operator_broker_ca", inp.operator_broker_ca),
        ):
            if "\x00" in value:
                raise ValueError(f"{label} must not contain NUL bytes")
    # The baked short-lived tokens also flow into box.env / a shell sink (P5-03 · G1-7).
    for label, tok in (("bootstrap_token", inp.bootstrap_token), ("callback_token", inp.callback_token)):
        if tok and not _TOKEN_RE.match(tok):
            raise ValueError(f"invalid {label} (charset ^[A-Za-z0-9._-]+$): {tok!r}")
    unknown = [m for m in inp.enabled_modules if m not in MODULE_IDS]
    if unknown:
        raise ValueError(f"unknown enabled modules: {sorted(unknown)}")
    for module_id in inp.enabled_modules:
        ref = inp.images.get(module_id)
        if not ref:
            raise ValueError(f"images map is missing an enabled module: {module_id}")
        err = validate_image_ref(ref)
        if err:
            raise ValueError(err)
        if inp.role == "customer":
            version = str(inp.module_versions.get(module_id, "")).strip()
            if not version or "\n" in version or "\r" in version or len(version) > 64:
                raise ValueError(f"module_versions is missing a safe version for enabled module: {module_id}")
    if inp.role == "customer":
        if not inp.release_version or "\n" in inp.release_version or "\r" in inp.release_version or len(inp.release_version) > 64:
            raise ValueError("release_version is required and must be a safe metadata value")
        if "\n" in inp.release_migration or "\r" in inp.release_migration or len(inp.release_migration) > 64:
            raise ValueError("release_migration must be a safe metadata value")
    if inp.customer_bootstrap:
        if inp.role != "customer":
            raise ValueError("customer_bootstrap is only valid for customer boxes")
        descriptor = decode_customer_bootstrap(inp.customer_bootstrap)
        if descriptor is None or descriptor.account_id != inp.account_id:
            raise ValueError("customer_bootstrap account does not match the rendered box")


def _validate_callback_url_template(value: str) -> None:
    """Defend the cloud-init shell sink even when dispatch bypasses HTTP validation."""
    cleaned = value.strip()
    if any(char in cleaned for char in "$`()|;<>\\'\" \t\n\r"):
        raise ValueError("callback_url contains unsafe shell characters")
    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise ValueError("callback_url must be a valid absolute https URL") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("callback_url must be an absolute https URL without credentials")
    if "{run_id}" not in cleaned:
        raise ValueError("callback_url must contain the {run_id} placeholder")


def _ordered(enabled) -> list:
    present = set(enabled)
    return [m for m in MODULE_ORDER if m in present]


def _enabled_products(enabled) -> list:
    present = {_PRODUCT_OF[m] for m in _ordered(enabled)}
    return [p for p in PRODUCTS if p in present]


def enabled_product_dbs(enabled_modules) -> tuple:
    """The Postgres DB names for the enabled products — the pg_dump targets the offsite backup
    agent iterates (BK3/BK5). Derived from the same product set the compose + Caddy use, so the
    backup set can never drift from what is actually deployed on the box."""
    return tuple(_DB_OF[p] for p in _enabled_products(enabled_modules))


def _migrate_included(inp: BoxRenderInputs, product: str) -> bool:
    base, _, _ = _MIGRATE[product]
    return base in inp.enabled_modules


def _urlencoded_secret_ref(password_env: str) -> str:
    """Return the derived dotenv alias used for a password inside a URI.

    The raw variables remain in the infra env files for ``postgres-init.sh`` and
    Redis itself.  ``render_dotenv`` derives these aliases from the same raw
    values, so a reserved character cannot change the URL's authority/path.
    """
    return f"{password_env}_URLENCODED"


def _db_url(product: str) -> str:
    return (
        f"postgresql://{_PG_USER}:${{{_urlencoded_secret_ref('POSTGRES_PASSWORD')}}}"
        f"@postgres:5432/{_DB_OF[product]}"
    )


def _role_db_url(role: str, password_env: str, product: str = "onebrain") -> str:
    return (
        f"postgresql://{role}:${{{_urlencoded_secret_ref(password_env)}}}"
        f"@postgres:5432/{_DB_OF[product]}"
    )


def _redis_url(password_env: str = "REDIS_PASSWORD") -> str:
    return f"redis://:${{{_urlencoded_secret_ref(password_env)}}}@redis:6379"


def _needs_redis(module_id: str) -> bool:
    return "REDIS_URL" in MODULE_ENV_REQUIREMENTS.get(module_id, ())


def _is_http(module_id: str) -> bool:
    probe = MODULE_HEALTH_PROBES.get(module_id)
    return bool(probe and probe.kind == "http")


# --- compose -----------------------------------------------------------------
def _compose_service(name, *, image, profiles=None, command=None, env_file=None, expose=None,
                     ports=None, volumes=None, depends=None, restart="unless-stopped", healthcheck=None,
                     runtime_hardening_anchor=None, tmpfs_override=None, networks=None) -> str:
    lines = [f"  {name}:", f"    image: {image}"]
    if profiles:
        lines.append(f"    profiles: [{', '.join(profiles)}]")
    if runtime_hardening_anchor:
        lines.append(f"    <<: *{runtime_hardening_anchor}")
    lines.append(f"    restart: {restart}")
    if tmpfs_override:
        lines.append("    tmpfs: [" + ", ".join(f'\"{mount}\"' for mount in tmpfs_override) + "]")
    if command is not None:
        # JSON-array form; escape embedded double quotes so a shell -c argument that
        # itself quotes an env ref (redis) renders as valid YAML/JSON.
        lines.append("    command: [" + ", ".join('"' + c.replace('"', '\\"') + '"' for c in command) + "]")
    if env_file is not None:
        lines.append("    env_file:")
        lines.append(f"      - {env_file}")
    if ports:
        # PUBLISHED host ports (the ingress ONLY). Every other service uses `expose` so the
        # Hetzner Cloud Firewall is the sole inbound path; Caddy is the deliberate exception.
        lines.append("    ports:")
        for pub in ports:
            lines.append(f'      - "{pub}"')
    if expose:
        lines.append("    expose:")
        lines.append(f'      - "{expose}"')
    if volumes:
        lines.append("    volumes:")
        for vol in volumes:
            lines.append(f"      - {vol}")
    if networks:
        lines.append("    networks:")
        for network, config in networks:
            lines.append(f"      {network}: {config}" if config else f"      {network}: {{}}")
    if depends:
        lines.append("    depends_on:")
        for dep_name, condition in depends:
            lines.append(f"      {dep_name}:")
            lines.append(f"        condition: {condition}")
    if healthcheck:
        lines.append("    healthcheck:")
        for hc_line in healthcheck:
            lines.append(f"      {hc_line}")
    return "\n".join(lines)


_ONEBRAIN_RUNTIME_TMPFS = ("/tmp:mode=1777,size=64m",)
_ONEBRAIN_WEB_RUNTIME_TMPFS = _ONEBRAIN_RUNTIME_TMPFS + (
    "/app/.next/cache:mode=1777,size=64m",
)
_ONEBRAIN_WORKER_RUNTIME_TMPFS = (
    # Archive inspection is capped at 512 MiB; leave bounded headroom for the
    # scanner subprocess and keep all scratch data memory-backed.
    "/tmp:mode=1777,size=640m",
)
_ONEBRAIN_RUNTIME_CAP_DROP = ("ALL",)
_ONEBRAIN_RUNTIME_SECURITY_OPT = ("no-new-privileges:true",)
# Kept short because this alias is embedded in Hetzner's size-limited cloud-init.
_ONEBRAIN_RUNTIME_ANCHOR = "x"


def _onebrain_runtime_hardening() -> dict:
    """Compose restrictions for the images maintained in this repository.

    The images themselves declare the non-root runtime identity. Only API/worker
    services bind-mount the persistent `/data` state directory. Customer API
    and workers also receive the dedicated attached-volume Drive path; every
    other filesystem write must use the explicit per-container `/tmp` tmpfs.
    """

    return {"runtime_hardening_anchor": _ONEBRAIN_RUNTIME_ANCHOR}


def _onebrain_runtime_hardening_yaml() -> str:
    """A shared YAML merge anchor keeps Hetzner user-data comfortably bounded."""

    return (
        f"x-{_ONEBRAIN_RUNTIME_ANCHOR}: &{_ONEBRAIN_RUNTIME_ANCHOR} "
        "{read_only: true, "
        "tmpfs: [" + ", ".join(f'\"{mount}\"' for mount in _ONEBRAIN_RUNTIME_TMPFS) + "], "
        f"cap_drop: [{', '.join(_ONEBRAIN_RUNTIME_CAP_DROP)}], "
        f"security_opt: [{', '.join(_ONEBRAIN_RUNTIME_SECURITY_OPT)}]}}"
    )


def render_compose(inp: BoxRenderInputs) -> str:
    _validate(inp)
    ordered = _ordered(inp.enabled_modules)
    products = _enabled_products(inp.enabled_modules)
    api_enabled = "onebrain-api" in ordered
    blocks = [_onebrain_runtime_hardening_yaml(), "services:"]

    # Ingress: Caddy is the ONE public entrypoint. It PUBLISHES 80/443 (the only service
    # that does — everything else is `expose`-only), terminates TLS (auto-HTTPS via Let's
    # Encrypt when a fqdn is set; plain :80 otherwise), and reverse-proxies to the internal
    # services per the rendered Caddyfile. NO profile: the ingress runs regardless of which
    # product profiles are enabled, so 80/443 are bound the moment the box boots. Without
    # this service nothing listens on 80/443 and every box reports CONNECTION REFUSED even
    # though the app containers are healthy. Certs/keys persist on host paths so a restart
    # never re-hits ACME rate limits. No env_file, no depends_on: Caddy retries upstreams
    # until they resolve, so it never blocks on (profiled) app services being up first.
    blocks.append(_compose_service(
        "caddy",
        image=_CADDY_IMAGE,
        ports=["80:80", "443:443"],
        volumes=[
            "/opt/onebrain/Caddyfile:/etc/caddy/Caddyfile:ro",
            "/opt/onebrain/caddy-data:/data",
            "/opt/onebrain/caddy-config:/config",
        ],
        networks=(
            ("default", ""),
            (_EDGE_NETWORK, f"{{ipv4_address: {_CADDY_EDGE_IP}}}"),
        ) if api_enabled else None,
    ))

    # Infra: one postgres (no profile, one data volume, three product DBs via
    # the init script), one redis. expose only (never ports) so Docker's iptables
    # cannot bypass the host firewall.
    blocks.append(_compose_service(
        "postgres",
        # pgvector-enabled Postgres 16 (drop-in superset of postgres:16): migration 0001 runs
        # `CREATE EXTENSION vector`, which the stock postgres:16 image lacks (initdb has no
        # vector.control) -> migrate would exit 1 and the whole app never starts.
        image=_PGVECTOR_IMAGE,
        env_file="env/postgres.env",
        expose="5432",
        volumes=[
            "/mnt/onebrain-data:/var/lib/postgresql/data",
            "/opt/onebrain/postgres-init.sh:/docker-entrypoint-initdb.d/postgres-init.sh:ro",
        ],
        healthcheck=[
            # TCP + real DB (not the default unix socket): the official image runs
            # /docker-entrypoint-initdb.d against a socket-only server (listen_addresses='')
            # BEFORE opening TCP, so a socket probe can report healthy while migrate
            # services (depends_on service_healthy) get connection-refused on 5432.
            'test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -p 5432 -U onebrain -d onebrain"]',
            "interval: 10s",
            "timeout: 5s",
            "retries: 5",
        ],
    ))
    # The entrypoint init hook only executes on a brand-new volume. Run the
    # same idempotent role normalizer after Postgres is healthy so an upgraded
    # box also gets the restricted product logins and database ACLs before any
    # migration or long-running service can connect.
    blocks.append(_compose_service(
        "postgres-roles",
        image=_PGVECTOR_IMAGE,
        command=["sh", "-ec", "PGHOST=postgres exec /opt/onebrain/postgres-init.sh"],
        env_file="env/postgres.env",
        volumes=["/opt/onebrain/postgres-init.sh:/opt/onebrain/postgres-init.sh:ro"],
        depends=[("postgres", "service_healthy")],
        restart='"no"',
    ))
    blocks.append(_compose_service(
        "redis",
        image=_REDIS_IMAGE,
        # requirepass MUST resolve from the in-container REDIS_PASSWORD (env_file), not a
        # compose-time ${REDIS_PASSWORD} interpolation (which reads the shell/.env — empty
        # here, so redis would boot passwordless while clients AUTH). The doubled $$ escapes
        # compose interpolation, leaving $REDIS_PASSWORD for the container shell to expand —
        # same source the healthcheck and every client's REDIS_URL read.
        command=["sh", "-c", 'exec redis-server --requirepass "$$REDIS_PASSWORD"'],
        env_file="env/redis.env",
        expose="6379",
        healthcheck=[
            'test: ["CMD-SHELL", "redis-cli -a \\"$$REDIS_PASSWORD\\" ping | grep -q PONG"]',
            "interval: 10s",
            "timeout: 5s",
            "retries: 5",
        ],
    ))

    for product in products:
        migrate_present = _migrate_included(inp, product)
        migrate_name = f"{product}-migrate"
        if migrate_present:
            base, command, _ = _MIGRATE[product]
            blocks.append(_compose_service(
                migrate_name,
                image=inp.images[base],
                profiles=[product],
                command=command,
                env_file=f"env/{migrate_name}.env",
                depends=[("postgres-roles", "service_completed_successfully")],
                restart='"no"',
                **(_onebrain_runtime_hardening() if product == "onebrain" else {}),
            ))
        for module_id in ordered:
            if _PRODUCT_OF[module_id] != product:
                continue
            probe = MODULE_HEALTH_PROBES.get(module_id)
            expose = str(probe.port) if (probe and probe.kind == "http") else None
            volumes = ["/data:/data"] if module_id in ("onebrain-api", "onebrain-workers") else None
            if module_id == "onebrain-workers":
                # Persistent private definition cache. It is deliberately on
                # the host root disk, not the customer-data volume or Drive
                # subtree, so backup/export/erasure cannot absorb signatures.
                volumes = (volumes or []) + [
                    f"{_MALWARE_DEFINITION_CACHE_DIR}:{_MALWARE_DEFINITION_CACHE_DIR}"
                ]
            if volumes is not None and inp.role != "operator":
                # Drive is an always-on onebrain_core capability on customer boxes. Originals
                # live on the attached data volume, isolated from the legacy root-disk /data
                # state. Mission Control deliberately receives no Drive filesystem surface.
                volumes.append("/mnt/onebrain-data/drive:/data/drive")
            if (
                module_id == "onebrain-api"
                and inp.role == "operator"
                and inp.operator_broker_client_certificate
            ):
                # Only the MC API calls the remote provisioning broker. Do not
                # expose its client certificate/key to workers, migration jobs,
                # the UI, Caddy, or any customer-shaped box.
                volumes = (volumes or []) + [
                    f"{_OPERATOR_TLS_HOST_DIR}:{_OPERATOR_TLS_CONTAINER_DIR}:ro"
                ]
            # Every application profile waits for the role normalizer, even
            # when its module set does not include a migration container. This
            # prevents a worker-only/external-product profile from receiving a
            # restricted DSN before its login and database ACLs exist.
            depends = [("postgres-roles", "service_completed_successfully")]
            if _needs_redis(module_id):
                depends.append(("redis", "service_healthy"))
            if migrate_present:
                depends.append((migrate_name, "service_completed_successfully"))
            blocks.append(_compose_service(
                module_id,
                image=inp.images[module_id],
                profiles=[product],
                env_file=f"env/{module_id}.env",
                expose=expose,
                volumes=volumes,
                depends=depends,
                **(_onebrain_runtime_hardening() if product == "onebrain" else {}),
                tmpfs_override=(
                    _ONEBRAIN_WEB_RUNTIME_TMPFS
                    if module_id == "onebrain-admin-ui"
                    else _ONEBRAIN_WORKER_RUNTIME_TMPFS
                    if module_id == "onebrain-workers"
                    else None
                ),
                networks=(
                    ("default", ""),
                    (_EDGE_NETWORK, f"{{aliases: [{_API_EDGE_ALIAS}]}}"),
                ) if module_id == "onebrain-api" else None,
            ))
    if api_enabled:
        blocks.append(
            f"networks:\n  {_EDGE_NETWORK}: {{internal: true, ipam: {{config: [{{subnet: {_EDGE_SUBNET}}}]}}}}"
        )
    return "\n".join(blocks) + "\n"


# --- env files ---------------------------------------------------------------
def _kv(pairs) -> str:
    return "\n".join(f"{k}={v}" for k, v in pairs) + "\n"


def _module_env(module_id: str, inp: BoxRenderInputs) -> list:
    """Ordered (key, value) pairs for one service's env file. Secrets are ALWAYS
    ${VAR} refs (never plaintext)."""
    refs = inp.secret_refs
    pairs: list = []
    if module_id in ("onebrain-api", "onebrain-workers"):
        pairs += [("ONEBRAIN_VECTOR_STORE", "pgvector"),
                  ("ONEBRAIN_DATABASE_URL", _role_db_url(
                      inp.postgres_app_role, refs.app_db_password_env)),
                  ("ONEBRAIN_POSTGRES_APP_ROLE", inp.postgres_app_role),
                  ("ONEBRAIN_POSTGRES_WORKER_ROLE", inp.postgres_worker_role),
                  ("ONEBRAIN_DATA_DIR", "/data"),
                  # Production-boot essentials, baked on BOTH the api and the worker (they open
                  # the same tenant Postgres). ONEBRAIN_ENVIRONMENT=production makes
                  # settings.is_production_like True, which ARMS validate_runtime_safety's net
                  # (pgvector + a real DSN + RLS) instead of silently skipping it on a box that
                  # otherwise defaults to the dev environment. ONEBRAIN_RLS_ENFORCED=true then
                  # enforces Postgres row-level security so tenant isolation is ON — mandatory
                  # for a multi-tenant customer box (and required once production-like). Fixed
                  # literals (not per-box secrets), so they live in the render, not the bundle.
                  ("ONEBRAIN_ENVIRONMENT", "production"),
                  ("ONEBRAIN_RLS_ENFORCED", "true"),
                  # Both the API and worker construct production-like settings.
                  # The worker does not sign cookies, but it must receive this
                  # dedicated HMAC secret to pass the same runtime safety gate.
                  (f"{refs.login_rate_limit_secret_env}",
                   "${" + refs.login_rate_limit_secret_env + "}"),
                  ("ONEBRAIN_DRIVE_DATA_DIR", "/data/drive"),
                  ("ONEBRAIN_DRIVE_POLICY_MODE", inp.drive_policy_mode),
                  ("ONEBRAIN_DRIVE_PRIVATE_SPACES_ENABLED", "false"),
                  ("ONEBRAIN_PII_PHASE", inp.pii_phase)]
        if module_id == "onebrain-workers":
            pairs.append(("ONEBRAIN_WORKER_DATABASE_URL", _role_db_url(
                inp.postgres_worker_role, refs.worker_db_password_env)))
            # Malware quarantine is a standard OneBrain capability on every
            # production customer worker.  There is deliberately no rendered
            # disable switch; local/test stacks retain the fake adapter default.
            pairs.append(("ONEBRAIN_DRIVE_MALWARE_SCANNER", "clamav"))
            # Stable operational row identity; queue claim/lease worker IDs
            # remain per-process and are intentionally separate.
            pairs.append(("ONEBRAIN_DRIVE_MALWARE_WORKER_ID", "worker_primary"))
            # Worker startup validates a distinct queue-only login. Mark this
            # container explicitly so deployment safety never mistakes it for
            # an API replica merely because ONEBRAIN_PROCESS defaults to api.
            pairs.append(("ONEBRAIN_PROCESS", "worker"))
    if module_id == "onebrain-api":
        pairs += [
            ("ONEBRAIN_DEPLOYMENT_ID", inp.deployment_id),
            (f"{refs.llm_key_env}", "${" + refs.llm_key_env + "}"),
            # The session-cookie signing secret. app/main.py FAILS CLOSED (RuntimeError,
            # refuses to boot) unless this is a strong (>=32-char) non-default value, so it is
            # a bundle SECRET — a fresh per-box secrets.token_hex(32) minted by the MC/customer
            # bundle — delivered via /opt/onebrain/.env, the SAME ${VAR} mechanism as
            # ONEBRAIN_ADMIN_PASSWORD. Only onebrain-api validates/signs with it, so it is NOT
            # baked on the worker (whose entrypoint never constructs the app).
            (f"{refs.auth_secret_env}", "${" + refs.auth_secret_env + "}"),
            # The admin seed pair. seed.py (seed_admin_from_env) creates a loginable admin
            # at container start ONLY when BOTH are non-empty; the box fills them from the
            # exchanged (customer) / baked (MC) /opt/onebrain/.env. Without the email the box
            # seeds no admin and — SSH closed — is unreachable. Only onebrain-api seeds.
            (f"{refs.admin_email_env}", "${" + refs.admin_email_env + "}"),
            (f"{refs.owner_bootstrap_env}", "${" + refs.owner_bootstrap_env + "}"),
            # Every box is fronted by Caddy TLS, so session cookies must carry the Secure flag.
            ("ONEBRAIN_COOKIE_SECURE", "true"),
            # Caddy is the only peer on the private edge network. It overwrites
            # X-Forwarded-For from the socket peer before proxying, so this trust
            # cannot be forged by another application container.
            ("ONEBRAIN_TRUSTED_PROXY_CIDRS", f"{_CADDY_EDGE_IP}/32"),
            ("ONEBRAIN_TRUSTED_PROXY_HOPS", "1"),
            ("ONEBRAIN_MODULE_PROBES_ENABLED", "true"),
            ("ONEBRAIN_LOCAL_MODULES", ",".join(_ordered(inp.enabled_modules))),
        ]
        if inp.role == "customer":
            # A customer-shaped box must not be able to turn into a control plane
            # merely because the framework defaults change. The root-only host
            # agent (not this Compose service) owns the fleet credential and
            # reports the release-gate heartbeat.
            pairs += [
                ("ONEBRAIN_CUSTOMER_BOOTSTRAP", inp.customer_bootstrap),
                (refs.assistant_service_key_env, "${" + refs.assistant_service_key_env + "}"),
                (refs.communication_service_key_env, "${" + refs.communication_service_key_env + "}"),
                ("ONEBRAIN_OPERATOR_MODE", "false"),
                ("ONEBRAIN_OPERATOR_CONSOLE", "false"),
                ("ONEBRAIN_FLEET_REPORTER_ENABLED", "false"),
            ]
    if module_id == "onebrain-admin-ui":
        pairs += [("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000")]
    if module_id == "assistant-service":
        pairs += [
            ("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000"),
            (refs.service_key_env, "${" + refs.assistant_service_key_env + "}"),
            ("DATABASE_URL", _role_db_url(
                inp.postgres_assistant_role, refs.assistant_db_password_env, "assistant")),
            ("REDIS_URL", _redis_url(refs.redis_password_env)),
        ]
    if module_id in ("communication-api", "communication-workers"):
        pairs += [
            ("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000"),
            (refs.service_key_env, "${" + refs.communication_service_key_env + "}"),
            (refs.space_id_env, "${" + refs.communication_space_id_env + "}"),
        ]
        if module_id == "communication-api":
            pairs += [("ONEBRAIN_ACCOUNT_ID", inp.account_id)]
        pairs += [("DATABASE_URL", _role_db_url(
            inp.postgres_communication_role, refs.communication_db_password_env, "communication")),
                  ("REDIS_URL", _redis_url(refs.redis_password_env))]
    if module_id == "communication-voice":
        pairs += [("DATABASE_URL", _role_db_url(
            inp.postgres_communication_role, refs.communication_db_password_env, "communication")),
                  ("REDIS_URL", _redis_url(refs.redis_password_env))]
    if module_id == "communication-widget":
        pairs += [("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000")]
    selector = _COMMUNICATION_SERVICE_SELECTORS.get(module_id)
    if selector:
        pairs.append(("SERVICE", selector))
    # A14 operator overlay (dormant in P4; only onebrain-api carries it).
    if module_id == "onebrain-api" and inp.role == "operator":
        pairs += [
            # The whole Mission Control surface (fleet router, G3-2 self-seed, P5-04
            # scheduler) is gated on settings.operator_mode. ONEBRAIN_IS_OPERATOR_SURFACE
            # is a READ-ONLY @property derived FROM operator_mode/operator_console — the env
            # var does NOT set operator_mode, so it alone leaves the MC box with no fleet
            # surface. ONEBRAIN_OPERATOR_MODE is the settable field that actually arms MC;
            # bake it true (and its own public URL) so the single go-live command yields a
            # live, self-enrolled, heartbeating MC.
            ("ONEBRAIN_OPERATOR_MODE", "true"),
            ("ONEBRAIN_OPERATOR_CONSOLE", "true"),
            ("ONEBRAIN_IS_OPERATOR_SURFACE", "true"),
            ("ONEBRAIN_FLEET_REPORTER_ENABLED", "true"),
            ("ONEBRAIN_FLEET_URL", inp.fleet_url),
            (f"{refs.fleet_key_env}", "${" + refs.fleet_key_env + "}"),
            ("ONEBRAIN_FLEET_PUBLIC_URL", inp.fleet_url),
            # The MC API is the intentionally privileged control-plane surface.
            # Customer API containers never receive the owner connection.
            ("ONEBRAIN_OPERATOR_DATABASE_URL", _db_url("onebrain")),
            # Production MC is deliberately remote-broker-only. The initial
            # bootstrap CLI may hold a one-time Hetzner token, but this API
            # container never does: it gets only the scoped broker credential
            # plus the read-only mTLS files mounted below.
            ("ONEBRAIN_PROVISIONER_BACKEND", "hetzner"),
            ("ONEBRAIN_HETZNER_ALLOW_INPROCESS_BROKER", "false"),
            ("ONEBRAIN_HETZNER_BROKER_URL", inp.operator_broker_url),
            (refs.broker_credential_env, "${" + refs.broker_credential_env + "}"),
            ("ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS",
             "${ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS}"),
            # MC stores customer secret bundles. This is its own escrowed Fernet
            # key, not a customer-bundle value.
            (refs.secret_encryption_key_env, "${" + refs.secret_encryption_key_env + "}"),
            ("ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY",
             "${ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY}"),
            # G1-1 interlock input: the APP-level accepted wrapper-key SET the box verifies
            # its OWN active signer against at startup. Without this the MC box has an EMPTY
            # served set while signing with the private key above -> active_signer_in_served_set()
            # is False -> onebrain-api RuntimeErrors on every boot (and /bootstrap 409s every
            # customer bundle). A ${VAR} ref filled from the operator .env (bootstrap_mc bakes
            # the value, which its preflight already asserts contains the derived active signer).
            ("ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS",
             "${ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS}"),
            ("ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY", inp.release_public_key),
            ("ONEBRAIN_RELEASE_REGISTRY_ALLOWLIST", inp.registry_allowlist),
            ("ONEBRAIN_RELEASE_REQUIRE_SIGNATURE", "true"),
            ("ONEBRAIN_RELEASE_REQUIRE_SIGNED_IMAGES", "true"),
            ("ONEBRAIN_RELEASE_REQUIRE_ROLLBACK_KIND", "true"),
            ("ONEBRAIN_RELEASE_PROMOTION_REQUIRED", "true"),
            ("ONEBRAIN_FLEET_RECONCILE_SECONDS", str(inp.operator_fleet_reconcile_seconds)),
        ]
        if inp.operator_broker_client_certificate:
            pairs += [
                ("ONEBRAIN_HETZNER_BROKER_CLIENT_CERTIFICATE_FILE",
                 f"{_OPERATOR_TLS_CONTAINER_DIR}/mc-client.crt"),
                ("ONEBRAIN_HETZNER_BROKER_CLIENT_KEY_FILE",
                 f"{_OPERATOR_TLS_CONTAINER_DIR}/mc-client.key"),
            ]
            if inp.operator_broker_ca:
                pairs.append((
                    "ONEBRAIN_HETZNER_BROKER_CA_FILE",
                    f"{_OPERATOR_TLS_CONTAINER_DIR}/broker-ca.crt",
                ))
    return pairs


def _migrate_env(product: str, inp: BoxRenderInputs) -> list:
    if product == "onebrain":
        return [("ONEBRAIN_VECTOR_STORE", "pgvector"),
                ("ONEBRAIN_DATABASE_URL", _db_url("onebrain")),
                ("ONEBRAIN_POSTGRES_APP_ROLE", inp.postgres_app_role),
                ("ONEBRAIN_POSTGRES_WORKER_ROLE", inp.postgres_worker_role),
                ("ONEBRAIN_DATA_DIR", "/data")]
    return [("DATABASE_URL", _db_url(product))]


def render_env_files(inp: BoxRenderInputs) -> dict:
    _validate(inp)
    out: dict = {}
    # Infra env (secrets are ${VAR} refs).
    out["env/postgres.env"] = _kv([
        ("POSTGRES_USER", _PG_USER),
        ("POSTGRES_PASSWORD", "${" + inp.secret_refs.db_password_env + "}"),
        ("POSTGRES_APP_ROLE", inp.postgres_app_role),
        ("POSTGRES_APP_PASSWORD", "${" + inp.secret_refs.app_db_password_env + "}"),
        ("POSTGRES_WORKER_ROLE", inp.postgres_worker_role),
        ("POSTGRES_WORKER_PASSWORD", "${" + inp.secret_refs.worker_db_password_env + "}"),
        ("POSTGRES_ASSISTANT_ROLE", inp.postgres_assistant_role),
        ("POSTGRES_ASSISTANT_PASSWORD", "${" + inp.secret_refs.assistant_db_password_env + "}"),
        ("POSTGRES_COMMUNICATION_ROLE", inp.postgres_communication_role),
        ("POSTGRES_COMMUNICATION_PASSWORD", "${" + inp.secret_refs.communication_db_password_env + "}"),
        ("POSTGRES_INITDB_ARGS", "--auth-host=scram-sha-256"),
        # The data volume is a fresh ext4 mount whose root holds a `lost+found`, so pointing
        # PGDATA at the mount root makes initdb refuse ("directory exists but is not empty").
        # Initialize into a SUBDIRECTORY of the mount instead (the image's documented fix).
        ("PGDATA", "/var/lib/postgresql/data/pgdata"),
    ])
    out["env/redis.env"] = _kv([("REDIS_PASSWORD", "${" + inp.secret_refs.redis_password_env + "}")])
    for product in _enabled_products(inp.enabled_modules):
        if _migrate_included(inp, product):
            out[f"env/{product}-migrate.env"] = _kv(_migrate_env(product, inp))
    for module_id in _ordered(inp.enabled_modules):
        out[f"env/{module_id}.env"] = _kv(_module_env(module_id, inp))
    return out


# --- Caddyfile ---------------------------------------------------------------
# publicly reverse-proxied HTTP modules -> (port, path matcher). Order matters
# (specific before the catch-all). Workers are internal (never public).
_CADDY_ROUTES = (
    # Browser, box, and server calls use Caddy's direct API route. Caddy replaces
    # inbound X-Forwarded-For with the socket peer before reaching the API edge.
    ("onebrain-api", 8000, "/api/*"),
    ("onebrain-api", 8000, "/health*"),
    ("assistant-service", 8000, "/assistant/*"),
    ("communication-api", 4000, "/comm/api/*"),
    ("communication-widget", 5174, "/comm/widget/*"),
    ("communication-voice", 4100, "/comm/voice/*"),
)

# A customer deployment never serves the Mission Control control plane. These
# explicit early denials remain useful even though the app does not mount the
# routers: they prevent a future routing/default change from proxying a control
# path through the generic API handler.
_CADDY_DENY_PATHS = (
    "/api/fleet",
    "/api/fleet/*",
    "/api/operator",
    "/api/operator/*",
    "/api/provisioning",
    "/api/provisioning/*",
    "/api/rollouts",
    "/api/rollouts/*",
)


def render_caddyfile(inp: BoxRenderInputs) -> str:
    _validate(inp)
    present = set(inp.enabled_modules)
    site = inp.fqdn if inp.fqdn else ":80"
    blocks = []
    if inp.role == "customer":
        blocks.extend(
            f'    handle {path} {{\n        respond "Not Found" 404\n    }}'
            for path in _CADDY_DENY_PATHS
        )
    if "onebrain-api" in present:
        # Keep existing callback/browser URLs working during the proxy cutover,
        # but rewrite directly at Caddy instead of passing through Next.js.
        blocks.append(
            "    handle /api/onebrain/* {\n"
            "        uri replace /api/onebrain /api\n"
            "        reverse_proxy {\n"
            f"            dynamic a {_API_EDGE_ALIAS} 8000 {{\n"
            "                refresh 5s\n"
            "            }\n"
            "            header_up X-Forwarded-For {remote_host}\n"
            "        }\n"
            "    }"
        )
    for module_id, port, path in _CADDY_ROUTES:
        if module_id in present:
            if module_id == "onebrain-api":
                blocks.append(
                    f"    handle {path} {{\n"
                    "        reverse_proxy {\n"
                    f"            dynamic a {_API_EDGE_ALIAS} {port} {{\n"
                    "                refresh 5s\n"
                    "            }\n"
                    "            header_up X-Forwarded-For {remote_host}\n"
                    "        }\n"
                    "    }"
                )
            else:
                blocks.append(f"    handle {path} {{\n        reverse_proxy {module_id}:{port}\n    }}")
    default = None
    if "onebrain-admin-ui" in present:
        default = ("onebrain-admin-ui", 3000)
    elif "onebrain-api" in present:
        default = (_API_EDGE_ALIAS, 8000)
    if default:
        if default[0] == _API_EDGE_ALIAS:
            blocks.append(
                f"    handle {{\n        reverse_proxy {{\n"
                f"            dynamic a {default[0]} {default[1]} {{\n"
                "                refresh 5s\n"
                "            }\n"
                "            header_up X-Forwarded-For {remote_host}\n"
                "        }\n    }"
            )
        else:
            blocks.append(f"    handle {{\n        reverse_proxy {default[0]}:{default[1]}\n    }}")
    template = (_DEPLOY_TEMPLATES / "Caddyfile.tmpl").read_text(encoding="utf-8")
    return template.replace("{{SITE_ADDRESS}}", site).replace("{{SERVICE_BLOCKS}}", "\n".join(blocks))


# --- cloud-init --------------------------------------------------------------
def _read_box_file(name: str) -> str:
    # text mode: universal newlines collapse any CR to LF, so an embed is always LF.
    return (_DEPLOY_BOX / name).read_text(encoding="utf-8")


def _yaml_block(content: str, indent: str = "      ") -> str:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return "\n".join((indent + line) if line.strip() else "" for line in lines)


# Hetzner Cloud's API rejects user_data over 32768 bytes (422 invalid_input) and serves it
# VERBATIM, so the WHOLE document cannot be gzip-compressed (cloud-init would receive
# undecodable base64). Instead, cloud-init's write_files module natively decompresses any
# entry carrying `encoding: gz+b64` (gzip then base64) back to its ORIGINAL bytes on write.
# Emitting the large/repetitive entries (the box scripts, the compose, the metadata-drop
# systemd unit) that way — while the document itself stays a plain `#cloud-config` — shrinks
# the payload well under the limit. Only entries at/above this many UTF-8 bytes are eligible
# (below it gzip's header + base64's 33% inflation swamp the savings, AND small config —
# Caddyfile, per-service env, the small units — stays plain so it's greppable in the rendered
# user_data); eligible entries still fall back to plain if gz+b64 somehow isn't smaller, and
# Non-secret large entries are eligible for compression. MC-only secret
# material is packed into its own opaque archive so bootstrap dry-runs can
# redact it atomically. The gate sits above the largest small-config entry
# (~900B) and below the metadata-drop unit (~1.8KB).
_GZB64_THRESHOLD = 1024


def _write_file_entry(path: str, content: str, permissions: str = "0644",
                      *, compressible: bool = True) -> str:
    """A cloud-init write_files entry for ``path`` with ``content`` and ``permissions``.

    When ``compressible`` (the default) AND the content is at/above ``_GZB64_THRESHOLD`` bytes,
    the entry is emitted with cloud-init's ``encoding: gz+b64`` IFF that is strictly smaller
    than the plain form — true for the large/repetitive entries (box scripts, compose, the
    metadata-drop unit). Small entries stay plain (below the threshold, gzip's header + base64's
    33% inflation would swamp the savings, and small config stays greppable). cloud-init writes
    the DECOMPRESSED (original) bytes to disk either way, so the on-box file — content AND
    permissions — is byte-identical to the plain form. gzip mtime is pinned to 0 (+ a
    platform-independent OS byte) so the render is byte-for-byte reproducible across dev/CI.

    ``compressible=False`` keeps an entry in normal YAML form when needed.
    MC-only secret material instead uses the dedicated opaque archive created
    below, which ``bootstrap_mc._redact`` masks as one unit. Customer
    ``box.env`` remains a 0600 member of the normal asset archive to meet
    Hetzner's size limit."""
    plain = (
        f"  - path: {path}\n"
        f"    permissions: '{permissions}'\n"
        f"    content: |\n"
        f"{_yaml_block(content)}\n"
    )
    if not compressible or len(content.encode("utf-8")) < _GZB64_THRESHOLD:
        return plain
    # b64encode over gzip(mtime=0) -> a single-line ASCII scalar of the base64 alphabet
    # (A-Za-z0-9+/=), which is safe as an unquoted YAML plain scalar: no space, colon,
    # '#', '|', or newline, and gzip's base64 always starts "H4sI" (never a YAML indicator).
    blob = base64.b64encode(gzip.compress(content.encode("utf-8"), mtime=0)).decode("ascii")
    gzb64 = (
        f"  - path: {path}\n"
        f"    permissions: '{permissions}'\n"
        f"    encoding: gz+b64\n"
        f"    content: {blob}\n"
    )
    return gzb64 if len(gzb64) < len(plain) else plain


def _asset_archive(entries: list[tuple[str, str, str]]) -> bytes:
    """Build a deterministic tar containing non-secret box assets.

    A full stack otherwise crosses Hetzner's 32 KiB cloud-init limit once the
    root-only release agent is added. The archive contains only scripts, rendered
    configuration, metadata, and `${VAR}` placeholders. For customer boxes it also
    carries the root-only short-lived bootstrap/callback `box.env`; the file remains
    mode 0600 after extraction. Operator secrets deliberately use the separate
    MC-only archive so dry-run diagnostics can redact the reversible blob atomically.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path, content, permissions in entries:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size = len(data)
            info.mode = int(permissions, 8)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# The ordinary source files deliberately keep their operational comments: they are
# reviewed and run directly from the repository.  Cloud-init, however, has a hard
# 32 KiB request limit, and the rendered artifact carries a copy of those host
# tools solely to execute them.  Drop only standalone comments from that *copy*
# before archiving.  Never touch inline comments, data, or heredoc bodies.
_SHELL_HEREDOC_RE = re.compile(
    r"<<-?\s*(?:'(?P<single>[^']+)'|\"(?P<double>[^\"]+)\"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def _compact_shell_asset(content: str) -> str:
    """Remove safe full-line shell comments while preserving heredoc data exactly.

    A naive ``line.lstrip().startswith('#')`` rewrite can silently alter an
    embedded Python/SQL heredoc.  The small state machine below only strips
    comment lines while parsing shell source; once a heredoc starts, every line
    is copied verbatim until its delimiter.  Unusual multiple-heredoc syntax is
    left untouched rather than guessed at.
    """
    lines = content.splitlines(keepends=True)
    compacted: list[str] = []
    heredoc_delimiter: str | None = None
    for index, line in enumerate(lines):
        logical = line.rstrip("\r\n")
        if heredoc_delimiter is not None:
            compacted.append(line)
            if logical.lstrip("\t") == heredoc_delimiter:
                heredoc_delimiter = None
            continue

        # Empty shell lines outside heredoc data do not affect execution and
        # consume scarce cloud-init bytes even after compression.
        if not logical.strip():
            continue

        # The source shebang is part of the executable contract.  All other
        # standalone comments are non-functional in shell source.
        if index and logical.lstrip().startswith("#"):
            continue
        compacted.append(
            line if index == 0 and line.startswith("#!") else line.lstrip(" \t")
        )

        # Detect a conventional one-delimiter heredoc on executable source. If
        # an asset needs more elaborate shell syntax later, keep it unmodified
        # rather than risk treating source as data incorrectly.
        matches = list(_SHELL_HEREDOC_RE.finditer(logical))
        if len(matches) == 1:
            match = matches[0]
            heredoc_delimiter = next(
                value for value in (match.group("single"), match.group("double"), match.group("bare"))
                if value is not None
            )
        elif len(matches) > 1:
            return content
    return "".join(compacted)


def _compact_python_asset(content: str) -> str:
    """Remove non-executable Python documentation from an archive copy.

    The deployed helpers inspect neither ``__doc__`` nor function annotations.
    Dropping both keeps the host artifact below Hetzner's user-data ceiling while
    leaving the reviewed repository source intact.  Keep postponed-annotation
    imports, however: variable and class annotations remain executable and may
    refer to names available only during type checking.
    """
    try:
        compact_tree = ast.parse(content)
        for compact_node in ast.walk(compact_tree):
            if not isinstance(
                compact_node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            compact_body = compact_node.body
            if (
                compact_body
                and isinstance(compact_body[0], ast.Expr)
                and isinstance(compact_body[0].value, ast.Constant)
                and isinstance(compact_body[0].value.value, str)
            ):
                compact_body.pop(0)
            if isinstance(compact_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                compact_node.returns = None
                compact_node.type_comment = None
                arguments = compact_node.args
                annotated = [
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                ]
                if arguments.vararg:
                    annotated.append(arguments.vararg)
                if arguments.kwarg:
                    annotated.append(arguments.kwarg)
                for argument in annotated:
                    argument.annotation = None
        ast.fix_missing_locations(compact_tree)
        compacted_source = ast.unparse(compact_tree) + "\n"
        if content.startswith("#!"):
            compacted_source = content.splitlines()[0] + "\n" + compacted_source
        compile(compacted_source, "<compact-host-asset>", "exec")
        return compacted_source
    except (IndentationError, SyntaxError, ValueError):
        pass

    lines = content.splitlines(keepends=True)
    removable: set[int] = set()
    string_lines: set[int] = set()
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Constant, ast.JoinedStr))
                and (not isinstance(node, ast.Constant) or isinstance(node.value, str))
                and hasattr(node, "end_lineno")
            ):
                string_lines.update(range(node.lineno - 1, node.end_lineno))
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if not body:
                continue
            candidate = body[0]
            if not (
                isinstance(candidate, ast.Expr)
                and isinstance(candidate.value, ast.Constant)
                and isinstance(candidate.value.value, str)
            ):
                continue
            start = candidate.lineno - 1
            end = candidate.end_lineno - 1
            # Removing a same-line docstring could erase executable code. Keep
            # it unless the AST span owns both complete physical line bounds.
            if lines[start][:candidate.col_offset].strip() or lines[end][candidate.end_col_offset:].strip():
                continue
            removable.update(range(start, end + 1))

        tokens = tokenize.generate_tokens(io.StringIO(content).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            line_index = token.start[0] - 1
            if line_index == 0 and lines[line_index].startswith("#!"):
                continue
            # A comment after code is documentation for that expression and is
            # intentionally retained.  This condition also excludes comments
            # represented as data inside a triple-quoted string.
            if not lines[line_index][:token.start[1]].strip():
                removable.add(line_index)
        removable.update(
            index for index, line in enumerate(lines)
            if not line.strip() and index not in string_lines
        )
    except (IndentationError, SyntaxError, tokenize.TokenError):
        # A malformed source asset should remain visible to its normal syntax
        # validation instead of being changed by the payload compactor.
        return content
    return "".join(line for index, line in enumerate(lines) if index not in removable)


def _compact_host_asset(path: str, content: str) -> str:
    """Return the behavior-preserving, size-conscious archive form of a host asset.

    This is deliberately limited to source/comment formats.  Rendered Compose,
    environment files, certificates, and other data remain byte-for-byte as the
    renderer created them.  Systemd ignores standalone ``#`` comments, while
    the shell/Python helpers above protect executable syntax and data blocks.
    """
    suffix = Path(path).suffix
    if suffix == ".sh":
        return _compact_shell_asset(content)
    if suffix == ".py":
        return _compact_python_asset(content)
    if suffix in {".service", ".timer"}:
        lines = content.splitlines(keepends=True)
        return "".join(
            line for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        )
    return content


def _write_asset_archive(path: str, contents: bytes, permissions: str = "0600") -> str:
    """Write a binary tar through cloud-init's gz+b64 decoder (which leaves the
    decompressed tar on disk). This is always smaller than one encoded entry per
    asset and is deterministic because the gzip mtime is fixed."""
    blob = base64.b64encode(gzip.compress(contents, mtime=0)).decode("ascii")
    return (
        f"  - path: {path}\n"
        f"    permissions: '{permissions}'\n"
        "    encoding: gz+b64\n"
        f"    content: {blob}\n"
    )


def _write_b85_xz_asset_archive(path: str, contents: bytes, permissions: str = "0600") -> str:
    """Write a deterministic XZ tar as a compact Base85 literal.

    Base64 expands the compressed customer archive by one third.  The bootstrap
    already installs Python 3, whose standard library decodes Base85 before
    ``tar`` extracts the exact same XZ bytes.  A YAML literal keeps the Base85
    alphabet data-only, while the XZ container remains reproducible.
    """
    compressed = lzma.compress(
        contents,
        format=lzma.FORMAT_XZ,
        filters=[{
            "id": lzma.FILTER_LZMA2,
            "preset": 9 | lzma.PRESET_EXTREME,
            "lc": 3,
            "lp": 0,
            "pb": 0,
        }],
    )
    blob = base64.b85encode(compressed).decode("ascii")
    return (
        f"  - path: {path}\n"
        f"    permissions: '{permissions}'\n"
        "    content: |\n"
        f"      {blob}\n"
    )


def _operator_broker_tls_assets(inp: BoxRenderInputs) -> list[tuple[str, str, str]]:
    """The MC's mTLS client material, kept out of normal Compose env files.

    A separate archive lets dry-run diagnostics redact one opaque secret-bearing
    entry without hiding the normal rendered assets. ``render_cloud_init``
    later fixes ownership to uid 10001, the API image's non-root runtime user.
    """
    if not inp.operator_broker_client_certificate:
        return []
    entries = [
        (f"{_OPERATOR_TLS_HOST_DIR}/mc-client.crt", inp.operator_broker_client_certificate, "0400"),
        (f"{_OPERATOR_TLS_HOST_DIR}/mc-client.key", inp.operator_broker_client_key, "0400"),
    ]
    if inp.operator_broker_ca:
        entries.append((f"{_OPERATOR_TLS_HOST_DIR}/broker-ca.crt", inp.operator_broker_ca, "0400"))
    return entries


def _yaml_sq(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _health_url(inp: BoxRenderInputs) -> str:
    """Return the externally reachable probe URL for a rendered box.

    Caddy redirects HTTP to HTTPS when the box has a domain.  Probing the
    loopback HTTP listener would therefore turn a healthy domain-backed box
    into a false failed first-boot/update signal.  IP-only boxes retain their
    local HTTP probe because they deliberately do not provision TLS.
    """
    if inp.fqdn:
        return f"https://{inp.fqdn}/health"
    return "http://127.0.0.1/health"


def _box_env(inp: BoxRenderInputs) -> str:
    products = " ".join(_enabled_products(inp.enabled_modules))
    pairs = [
        ("ONEBRAIN_FLEET_URL", inp.fleet_url),
        ("ONEBRAIN_DEPLOYMENT_ID", inp.deployment_id),
        ("ONEBRAIN_FLEET_KEY", "${" + inp.secret_refs.fleet_key_env + "}"),
        ("ONEBRAIN_RUN_ID", inp.run_id),
        ("UPDATE_RELEASE_PUBLIC_KEY", inp.release_public_key),
        ("UPDATE_DESIRED_STATE_PUBLIC_KEY", inp.fleet_public_desired_state_key),
        ("UPDATE_REGISTRY_ALLOWLIST", inp.registry_allowlist),
        ("UPDATE_DATA_DIR", "/data"),
        # Root-only update/bootstrap/reporter state and encrypted migration
        # backups live under the UUID-verified attached volume, never /data.
        ("ONEBRAIN_MAINTENANCE_DIR", "/mnt/onebrain-data/onebrain-maintenance"),
        ("UPDATE_COMPOSE_DIR", "/opt/onebrain"),
        ("UPDATE_COMPOSE_PROJECT", inp.compose_project),
        ("UPDATE_PROFILES", products),
        ("UPDATE_LOCAL_MODULES", ",".join(_ordered(inp.enabled_modules))),
        ("UPDATE_HEALTH_URL", _health_url(inp)),
        ("UPDATE_INITIAL_RELEASE_FILE", "/opt/onebrain/installed-release.json"),
        # Customer stacks report from the root-owned companion after the update
        # tick. Mission Control continues to use its in-app reporter.
        ("ONEBRAIN_GATE_AGENT_ENABLED", "true" if inp.role == "customer" else "false"),
        # A5: the per-box backup-encryption key + the owner OTP are ${VAR} refs filled
        # by the bootstrap exchange (/opt/onebrain/.env), sourced BEFORE box.env so
        # these re-expand to the delivered real values.
        ("UPDATE_BACKUP_KEY", "${" + inp.secret_refs.backup_key_env + "}"),
        ("ONEBRAIN_ADMIN_PASSWORD", "${" + inp.secret_refs.owner_bootstrap_env + "}"),
        # BK3: the offsite-backup master gate is ALWAYS baked (the agent reads it to no-op).
        ("ONEBRAIN_BACKUP_ENABLED", "true" if inp.backup_enabled else "false"),
        # G1-7: the callback token is BAKED (a real value, not a ${VAR} ref) so the
        # metadata-egress-block FAILURE callback authenticates before the exchange has
        # run. It stays OUT of the exchange bundle (never a BUNDLE_KEY).
        ("ONEBRAIN_PROVISIONING_CALLBACK_TOKEN", inp.callback_token),
    ]
    # BK3: the rest of the offsite-backup config is emitted ONLY when enabled, so an INERT box
    # (backups off — the default until a bucket is set) carries zero backup bloat in its plain
    # box.env. Non-secret settings are BAKED; the two S3 credentials are secrets delivered as
    # ${VAR} refs from the exchanged .env (same mechanism as UPDATE_BACKUP_KEY). The box derives
    # its per-deployment object prefix from ONEBRAIN_DEPLOYMENT_ID, so the prefix isn't delivered.
    if inp.backup_enabled:
        pairs += [
            ("ONEBRAIN_BACKUP_S3_ENDPOINT", inp.backup_s3_endpoint),
            ("ONEBRAIN_BACKUP_S3_BUCKET", inp.backup_s3_bucket),
            ("ONEBRAIN_BACKUP_S3_REGION", inp.backup_s3_region),
            ("ONEBRAIN_BACKUP_RETENTION_DAYS", str(inp.backup_retention_days)),
            ("ONEBRAIN_BACKUP_DBS", " ".join(inp.backup_dbs)),
            ("ONEBRAIN_BACKUP_S3_ACCESS_KEY", "${" + inp.secret_refs.backup_s3_access_key_env + "}"),
            ("ONEBRAIN_BACKUP_S3_SECRET_KEY", "${" + inp.secret_refs.backup_s3_secret_key_env + "}"),
        ]
    # P5-03: the single-use first-boot bootstrap token is baked ONLY for a customer box
    # (which exchanges it for /opt/onebrain/.env). The MC box (role=operator, G3-1) bakes
    # its .env directly and is never minted a token, so no exchange runs for it.
    if inp.role != "operator":
        pairs.append(("ONEBRAIN_BOOTSTRAP_TOKEN", inp.bootstrap_token))
    return _kv(pairs)


def _initial_release_descriptor(inp: BoxRenderInputs) -> str:
    """Provision-time metadata for the root-only reporter before any desired-state
    apply. It intentionally excludes images, credentials, customer data, and callbacks."""
    return json.dumps({
        "version": inp.release_version,
        "migration_to": inp.release_migration,
        "modules": {module_id: inp.module_versions.get(module_id, "") for module_id in _ordered(inp.enabled_modules)},
    }, sort_keys=True) + "\n"


_META = "169.254.169.254"


def _callback_curl(status: str, smoke: str, run_id: str, *, kind: str, callback_url: str = "") -> str:
    if kind not in {"failure", "completion"}:
        raise ValueError("callback kind is required")
    override = ""
    if callback_url:
        # _validate_callback_url_template rejects quotes and shell syntax, so the
        # preflighted URL remains one literal assignment even when it contains `&`.
        override = "ONEBRAIN_CALLBACK_URL='" + callback_url.strip().replace("{run_id}", run_id) + "' "
    return override + (
        f'ONEBRAIN_CALLBACK_STATUS="{status}" '
        f'ONEBRAIN_CALLBACK_SMOKE="{smoke}" '
        f'ONEBRAIN_CALLBACK_KIND="{kind}" '
        "/opt/onebrain/onebrain-gate-agent.sh --provision-callback"
    )


def _first_boot_script(commands: list[str]) -> str:
    """Render first-boot work as an extracted Bash helper.

    Cloud-init stores every runcmd string verbatim, which is costly under
    Hetzner's fixed 32 KiB user-data cap. The helper is part of the regular XZ
    asset archive instead. It deliberately has no global ``set -e``: that
    retains cloud-init's prior command-by-command, fail-soft behavior.
    """
    return "#!/usr/bin/env bash\n" + "\n".join(commands) + "\n"


def render_cloud_init(inp: BoxRenderInputs) -> str:
    _validate(inp)
    if not inp.run_id:
        raise ValueError("run_id is required to render cloud-init (the box callback URL needs it)")
    compose = render_compose(inp)
    caddy = render_caddyfile(inp)
    env_files = render_env_files(inp)

    assets: list[tuple[str, str, str]] = [
        ("/opt/onebrain/docker-compose.yml", compose, "0644"),
        ("/opt/onebrain/Caddyfile", caddy, "0644"),
        ("/opt/onebrain/installed-release.json", _initial_release_descriptor(inp), "0644"),
        ("/opt/onebrain/postgres-init.sh", _read_box_file("postgres-init.sh"), "0755"),
        ("/opt/onebrain/onebrain_dotenv.sh", _read_box_file("onebrain_dotenv.sh"), "0644"),
        ("/opt/onebrain/update.sh", _read_box_file("update.sh"), "0755"),
        ("/opt/onebrain/onebrain-gate-agent.sh", _read_box_file("onebrain-gate-agent.sh"), "0755"),
        ("/opt/onebrain/onebrain_box_verify.py", _read_box_file("onebrain_box_verify.py"), "0644"),
        ("/opt/onebrain/onebrain-data-volume.sh", _read_box_file("onebrain-data-volume.sh"), "0755"),
        ("/opt/onebrain/onebrain-host-maintenance.sh", _read_box_file("onebrain-host-maintenance.sh"), "0755"),
        ("/opt/onebrain/onebrain-postgres-collation.sh", _read_box_file("onebrain-postgres-collation.sh"), "0755"),
        ("/etc/systemd/system/onebrain-data-volume.service", _read_box_file("onebrain-data-volume.service"), "0644"),
        ("/etc/systemd/system/onebrain-update.service", _read_box_file("onebrain-update.service"), "0644"),
        ("/etc/systemd/system/onebrain-update.timer", _read_box_file("onebrain-update.timer"), "0644"),
        ("/etc/systemd/system/onebrain-host-maintenance.service", _read_box_file("onebrain-host-maintenance.service"), "0644"),
        ("/etc/systemd/system/onebrain-host-maintenance.timer", _read_box_file("onebrain-host-maintenance.timer"), "0644"),
        ("/etc/systemd/system/onebrain-metadata-drop.service", _read_box_file("onebrain-metadata-drop.service"), "0644"),
        ("/etc/tmpfiles.d/onebrain-malware.conf", _MALWARE_DEFINITION_TMPFILES, "0644"),
    ]
    # Mission Control bakes its own bundle and reports its health through the
    # in-app self-heartbeat. Customer boxes additionally need the root-only
    # reporter for their provisioning callbacks and post-update heartbeats.
    if inp.role != "operator":
        assets.extend([
            ("/opt/onebrain/onebrain_bootstrap.sh", _read_box_file("onebrain_bootstrap.sh"), "0755"),
            ("/opt/onebrain/onebrain_gate_report.py", _read_box_file("onebrain_gate_report.py"), "0755"),
            ("/etc/systemd/system/onebrain-drive-backup.service",
             _read_box_file("onebrain-drive-backup.service").replace(
                 "{{COMPOSE_PROJECT}}", inp.compose_project), "0644"),
            ("/etc/systemd/system/onebrain-drive-backup.timer",
             _read_box_file("onebrain-drive-backup.timer"), "0644"),
            ("/etc/systemd/system/onebrain-drive-erasure-ledger.service",
             _read_box_file("onebrain-drive-erasure-ledger.service").replace(
                 "{{COMPOSE_PROJECT}}", inp.compose_project), "0644"),
            ("/etc/systemd/system/onebrain-drive-erasure-ledger.timer",
             _read_box_file("onebrain-drive-erasure-ledger.timer"), "0644"),
            # Presence is the explicit customer/Drive capability flag consumed by the
            # volume verifier. Operator boxes never receive it or a Drive directory.
            ("/etc/onebrain-drive-enabled", "enabled\n", "0644"),
        ])
    # Rendered env files contain `${VAR}` references rather than secret values;
    # package them with the other non-secret assets. The real exchanged/baked
    # operator dotenv is in the separate MC-only secret archive below.
    assets.extend(
        (f"/opt/onebrain/{rel_path}", content, "0600")
        for rel_path, content in env_files.items()
    )
    # Customer user-data must stay below Hetzner's 32 KiB limit. Its short-lived
    # callback/bootstrap token is already delivered in that confidential channel;
    # placing the root-only file in the compressed archive saves enough overhead
    # without changing on-box content or permissions. Operator-only secret
    # configuration instead goes in the separately redacted archive below.
    if inp.role != "operator":
        assets.append(("/opt/onebrain/box.env", _box_env(inp), "0600"))
    operator_tls_assets = _operator_broker_tls_assets(inp)
    operator_secret_assets: list[tuple[str, str, str]] = []
    if inp.role == "operator":
        # The MC must bake its dotenv because it cannot self-exchange from an
        # empty DB. Keep all operator-only secrets in one compact archive: this
        # is encoding, not encryption, so bootstrap_mc redacts the whole entry.
        operator_secret_assets = [("/opt/onebrain/box.env", _box_env(inp), "0600")]
        if inp.dotenv:
            operator_secret_assets.append(("/opt/onebrain/.env", inp.dotenv, "0600"))
        operator_secret_assets.extend(operator_tls_assets)

    profile_flags = " ".join(f"--profile {p}" for p in _enabled_products(inp.enabled_modules))
    # Anchor EVERY first-boot compose call to the rendered file: cloud-init runcmd runs
    # with cwd '/', and Compose V2 would otherwise find no compose file and start
    # nothing. Mirrors update.sh's dc() wrapper (-f "$COMPOSE").
    compose_file = "/opt/onebrain/docker-compose.yml"
    compose_cmd = (
        f"docker compose --project-name {inp.compose_project} -f {compose_file} {profile_flags}".strip()
    )
    # The MC box has no provisioning-run record for its synthetic bootstrap id,
    # so its old callback posted back to itself and always 404ed. Its in-app
    # heartbeat is the authoritative readiness signal. Keep the complete
    # callback/reporting path on customer boxes only.
    customer_callbacks = inp.role != "operator"
    fail_cb = _callback_curl(
        "failed", "failed", inp.run_id, kind="failure", callback_url=inp.callback_url
    ) if customer_callbacks else ""
    done_cb = _callback_curl(
        "${ST}", "${SMOKE}", inp.run_id, kind="completion", callback_url=inp.callback_url
    ) if customer_callbacks else ""
    metadata_failure_callback = (
        f'; [ -z "$F" ] || {{ {fail_cb} || true; }}' if customer_callbacks else ""
    )
    smoke_callback = f"; {done_cb} || true" if customer_callbacks else ""
    first_boot_items = [
        "mkdir -p /opt/onebrain/env /opt/onebrain/caddy-data /opt/onebrain/caddy-config /data /mnt/onebrain-data "
        f"&& install -d -o 10001 -g 10001 -m 0700 {_MALWARE_DEFINITION_CACHE_DIR} "
        "&& chown 10001:10001 /data && chmod 750 /data",
        *([
            *([f"install -d -o 10001 -g 10001 -m 0700 {_OPERATOR_TLS_HOST_DIR}"] if operator_tls_assets else []),
            "tar -xf /opt/onebrain/mc-broker-tls.tar -C / && rm -f /opt/onebrain/mc-broker-tls.tar"
            + (
                f" && chown -R 10001:10001 {_OPERATOR_TLS_HOST_DIR} && chmod 0700 {_OPERATOR_TLS_HOST_DIR} "
                f"&& chmod 0400 {_OPERATOR_TLS_HOST_DIR}/*"
                if operator_tls_assets else ""
            ),
        ] if operator_secret_assets else []),
        # Persist the volume's UUID in fstab and require its verifier before
        # Docker. This prevents a reboot from silently starting Postgres against
        # the root-disk bind mount when the attached data volume is absent.
        "systemctl stop docker.service docker.socket >/dev/null 2>&1 || true",
        "bash /opt/onebrain/onebrain-data-volume.sh setup",
        "install -d -o root -g root -m 0700 /mnt/onebrain-data/onebrain-maintenance",
        "systemctl daemon-reload && systemctl enable onebrain-data-volume.service",
        "systemctl enable --now docker",
        # Capture the public IP for the callback BEFORE the metadata drop below.
        f"curl -sf http://{_META}/hetzner/v1/metadata/public-ipv4 > /opt/onebrain/box.instance 2>/dev/null || true",
        # A10: wait until dockerd has created the DOCKER-USER chain, BOUNDED (~120s, generous
        # enough for dockerd to come up on a fresh box) and — critically — on timeout we still
        # `break` and PROCEED, never hanging the boot. The probe is a plain `iptables -L`
        # (nft-safe: `iptables` is the iptables-nft backend on ubuntu-24.04) with NO `-w`, so a
        # transient xtables-lock contention just costs one extra bounded loop iteration rather
        # than blocking the wait; if the chain never appears the metadata-drop.service below
        # pre-creates it anyway.
        "i=0; until iptables -L DOCKER-USER -n >/dev/null 2>&1; do i=$((i+1)); "
        '[ "$i" -ge 120 ] && break; sleep 1; done',
        # A5: BOTH the bridge (DOCKER-USER) and the host (OUTPUT) egress drops to the metadata
        # endpoint. FAIL SOFT — report-and-continue, NO `exit 1`. Rationale: a transient insert
        # failure (an iptables-nft quirk, a DOCKER-USER chain timing/lock race) must NOT abort
        # cloud-init before `docker compose up`, or the whole box would be bricked (no services,
        # 80/443 CONNECTION REFUSED) over a defense-in-depth EGRESS rule. That tradeoff is wrong:
        # inbound is already default-denied by the Hetzner Cloud Firewall (only 80/443 open,
        # H-3), and the persistent onebrain-metadata-drop.service — ordered Before=docker.service,
        # started `--now` right below, and re-run on EVERY boot — is the AUTHORITATIVE drop that
        # reliably enforces the egress block. This in-memory insert is only a fast belt; on
        # failure we still POST the failure callback (keep the operator signal), then CONTINUE so
        # the box serves. `-w` makes the insert wait for the xtables lock (dockerd may hold it
        # briefly at startup) so a lock race is not mistaken for a real failure.
        # Attempt BOTH insertions before reporting a failure. Keeping the callback
        # once makes the bootstrap artifact fit under Hetzner's user-data limit
        # without changing the fail-soft security semantics.
        f"F=; iptables -w -I DOCKER-USER -d {_META} -j DROP || F=1; "
        f"iptables -w -I OUTPUT -d {_META} -j DROP || F=1{metadata_failure_callback}",
        # G1-6: persist BOTH drops across reboots (the -I rules above are in-memory only) AND
        # apply them NOW via the authoritative oneshot, so the egress block is enforced this boot
        # even when the fast inserts above failed soft.
        "systemctl enable --now onebrain-metadata-drop.service",
        # P5-03: fetch /opt/onebrain/.env via the single-use bootstrap token AFTER the
        # (now persisted) metadata drop and BEFORE compose pull/up, so compose interpolates
        # the delivered ${VAR} secrets. Customer box only — the MC box (role=operator)
        # bakes its .env directly (G3-1) and runs no exchange.
        *(["bash /opt/onebrain/onebrain_bootstrap.sh || true"] if inp.role != "operator" else []),
        f"{compose_cmd} pull",
        f"{compose_cmd} up -d",
        "systemctl enable --now onebrain-update.timer",
        "systemctl enable --now onebrain-host-maintenance.timer",
        *(["systemctl start onebrain-drive-erasure-ledger.service",
           "systemctl enable --now onebrain-drive-erasure-ledger.timer",
           "systemctl enable --now onebrain-drive-backup.timer"]
          if inp.role != "operator" else []),
        # Smoke + customer provisioning callback (bootstrap_password = the owner OTP).
        f'sleep 5; if curl -sf {_health_url(inp)} >/dev/null 2>&1; then ST=succeeded; SMOKE=passed; '
        f"else ST=failed; SMOKE=failed; fi{smoke_callback}",
    ]
    assets.append(("/opt/onebrain/onebrain-firstboot.sh", _first_boot_script(first_boot_items), "0755"))

    # Keep verbose first-boot orchestration in the compact regular archive;
    # cloud-init itself only extracts it and starts the helper. The separate
    # MC-only secret archive stays gzip-wrapped so bootstrap dry-runs can mask
    # its reversible payload as one opaque field.
    archive_assets = [
        (path, _compact_host_asset(path, content), permissions)
        for path, content, permissions in assets
    ]
    entries = [_write_b85_xz_asset_archive(
        _BOOTSTRAP_ASSET_B85, _asset_archive(archive_assets))]
    if operator_secret_assets:
        entries.append(_write_asset_archive(
            "/opt/onebrain/mc-broker-tls.tar", _asset_archive(operator_secret_assets)))
    write_files = "".join(entries).rstrip("\n")

    runcmd_items = [
        "python3 -c 'import base64,sys;sys.stdout.buffer.write(base64.b85decode(open(0,\"rb\").read().strip()))' "
        f"<{_BOOTSTRAP_ASSET_B85} | tar -xJf - -C / && rm -f {_BOOTSTRAP_ASSET_B85}",
        "bash /opt/onebrain/onebrain-firstboot.sh",
    ]
    runcmd = "\n".join("  - " + _yaml_sq(item) for item in runcmd_items)

    # Keep the reviewed template documented in source, but omit non-functional
    # YAML comments from the submitted user-data. ``#cloud-config`` is the
    # required cloud-init header and therefore remains the first line.
    template_lines = (_DEPLOY_TEMPLATES / "cloud-init.yaml.tmpl").read_text(
        encoding="utf-8").splitlines()
    template = "\n".join(
        line for index, line in enumerate(template_lines)
        if index == 0 or not line.lstrip().startswith("#")
    ) + "\n"
    return template.replace("{{WRITE_FILES}}", write_files).replace("{{RUNCMD}}", runcmd)

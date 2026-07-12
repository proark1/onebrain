"""Pure render layer (P4-02): deterministic text generation for one box from
(deployment, enabled modules, release manifest images, secret *references*).

Pure functions, golden-file tested, dependency-free (no PyYAML/Jinja — strings are
assembled directly). Ports come from `app.module_manifest.MODULE_HEALTH_PROBES`
(H-4), NEVER Railway's :8080. One compose file for all products, gated by PROFILES
keyed to the enabled modules (H-5). One-shot migrate services gate the long-running
services (H-6). Per-product databases on one Postgres (A13). Per-service env files
(no inline secrets). The renderer ONLY ever emits `${VAR}` references for secrets, so
a golden file never contains plaintext — the metadata-endpoint egress block (A5/A10)
bounds their exposure until the Phase-5 bootstrap-token exchange fills them.

Injection discipline: `deployment_id`/`compose_project`/`fqdn` pass a strict charset
check and `images` values pass `validate_image_ref`; anything failing raises
`ValueError` (never emits)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.controlplane.base import MODULE_IDS, validate_image_ref
from app.module_manifest import MODULE_ENV_REQUIREMENTS, MODULE_HEALTH_PROBES

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_BOX = _REPO_ROOT / "deploy" / "box"
_DEPLOY_TEMPLATES = _REPO_ROOT / "deploy" / "templates"

# operator-config/server-minted id charset (injection guard).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

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
    "onebrain": ("onebrain-api", ["alembic", "upgrade", "head"], "onebrain"),
    "communication": ("communication-api", ["pnpm", "db:migrate"], "communication"),
    "assistant": ("assistant-service", ["alembic", "upgrade", "head"], "assistant"),
}
_DB_OF = {"onebrain": "onebrain", "assistant": "assistant", "communication": "communication"}
_PG_USER = "onebrain"


@dataclass(frozen=True)
class SecretRefs:
    """References the box resolves at boot, NOT plaintext. In P4 these are env-file
    PLACEHOLDERS the bootstrap-token exchange (P1-E, OUT) fills; the RENDERER only
    ever emits ${VAR} refs so a golden file never contains a secret."""

    fleet_key_env: str = "ONEBRAIN_FLEET_KEY"
    llm_key_env: str = "ONEBRAIN_LLM_API_KEY"
    db_password_env: str = "POSTGRES_PASSWORD"
    redis_password_env: str = "REDIS_PASSWORD"
    owner_bootstrap_env: str = "ONEBRAIN_ADMIN_PASSWORD"
    service_key_env: str = "ONEBRAIN_SERVICE_KEY"
    space_id_env: str = "ONEBRAIN_SPACE_ID"
    backup_key_env: str = "UPDATE_BACKUP_KEY"   # A5: per-box client-side backup key; lives in box.env.


@dataclass(frozen=True)
class BoxRenderInputs:
    deployment_id: str
    account_id: str
    compose_project: str                 # "onebrain-<deployment_id>" (D-6 railway_environment_id)
    enabled_modules: tuple               # subset of MODULE_IDS (from DeploymentModule rows)
    images: dict                         # module_id -> registry/repo@sha256:...  (ReleaseManifest.images)
    fqdn: str = ""                       # <deployment_id>.<fleet_base_domain> ("" -> serve on IP, http only)
    fleet_url: str = ""                  # MC base URL (heartbeat + desired-state GET)
    fleet_public_desired_state_key: str = ""   # baked so the box verifies the wrapper (H-7)
    release_public_key: str = ""               # baked so the box verifies the offline release sig
    registry_allowlist: str = ""               # baked box-local allowlist (B2) — never envelope-supplied
    trust_proxy: int = 1                        # TRUST_PROXY hop count for the box's real proxy (Caddy = 1)
    role: str = "customer"                      # A14: "customer" | "operator" (operator overlay is dormant in P4)
    secret_refs: SecretRefs = field(default_factory=SecretRefs)


# --- validation --------------------------------------------------------------
def _validate(inp: BoxRenderInputs) -> None:
    for label, value in (("deployment_id", inp.deployment_id), ("compose_project", inp.compose_project)):
        if not _ID_RE.match(value or ""):
            raise ValueError(f"invalid {label} (charset ^[a-z0-9][a-z0-9._-]*$): {value!r}")
    if inp.fqdn and not _ID_RE.match(inp.fqdn):
        raise ValueError(f"invalid fqdn (charset ^[a-z0-9][a-z0-9._-]*$): {inp.fqdn!r}")
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


def _ordered(enabled) -> list:
    present = set(enabled)
    return [m for m in MODULE_ORDER if m in present]


def _enabled_products(enabled) -> list:
    present = {_PRODUCT_OF[m] for m in _ordered(enabled)}
    return [p for p in PRODUCTS if p in present]


def _migrate_included(inp: BoxRenderInputs, product: str) -> bool:
    base, _, _ = _MIGRATE[product]
    return base in inp.enabled_modules


def _db_url(product: str) -> str:
    return f"postgresql://{_PG_USER}:${{POSTGRES_PASSWORD}}@postgres:5432/{_DB_OF[product]}"


def _redis_url() -> str:
    return "redis://:${REDIS_PASSWORD}@redis:6379"


def _needs_redis(module_id: str) -> bool:
    return "REDIS_URL" in MODULE_ENV_REQUIREMENTS.get(module_id, ())


def _is_http(module_id: str) -> bool:
    probe = MODULE_HEALTH_PROBES.get(module_id)
    return bool(probe and probe.kind == "http")


# --- compose -----------------------------------------------------------------
def _compose_service(name, *, image, profiles=None, command=None, env_file, expose=None,
                     volumes=None, depends=None, restart="unless-stopped", healthcheck=None) -> str:
    lines = [f"  {name}:", f"    image: {image}"]
    if profiles:
        lines.append(f"    profiles: [{', '.join(profiles)}]")
    lines.append(f"    restart: {restart}")
    if command is not None:
        lines.append("    command: [" + ", ".join(f'"{c}"' for c in command) + "]")
    lines.append("    env_file:")
    lines.append(f"      - {env_file}")
    if expose:
        lines.append("    expose:")
        lines.append(f'      - "{expose}"')
    if volumes:
        lines.append("    volumes:")
        for vol in volumes:
            lines.append(f"      - {vol}")
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


def render_compose(inp: BoxRenderInputs) -> str:
    _validate(inp)
    ordered = _ordered(inp.enabled_modules)
    products = _enabled_products(inp.enabled_modules)
    blocks = ["services:"]

    # Infra: one postgres (no profile, one data volume, three product DBs via
    # the init script), one redis. expose only (never ports) so Docker's iptables
    # cannot bypass the host firewall.
    blocks.append(_compose_service(
        "postgres",
        image="postgres:16",
        env_file="env/postgres.env",
        expose="5432",
        volumes=[
            "/mnt/onebrain-data:/var/lib/postgresql/data",
            "/opt/onebrain/postgres-init.sh:/docker-entrypoint-initdb.d/postgres-init.sh:ro",
        ],
        healthcheck=[
            'test: ["CMD-SHELL", "pg_isready -U onebrain"]',
            "interval: 10s",
            "timeout: 5s",
            "retries: 5",
        ],
    ))
    blocks.append(_compose_service(
        "redis",
        image="redis:7",
        command=["redis-server", "--requirepass", "${REDIS_PASSWORD}"],
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
                depends=[("postgres", "service_healthy")],
                restart='"no"',
            ))
        for module_id in ordered:
            if _PRODUCT_OF[module_id] != product:
                continue
            probe = MODULE_HEALTH_PROBES.get(module_id)
            expose = str(probe.port) if (probe and probe.kind == "http") else None
            volumes = ["/data:/data"] if module_id in ("onebrain-api", "onebrain-workers") else None
            depends = [("postgres", "service_healthy")]
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
            ))
    return "\n".join(blocks) + "\n"


# --- env files ---------------------------------------------------------------
def _kv(pairs) -> str:
    return "\n".join(f"{k}={v}" for k, v in pairs) + "\n"


def _module_env(module_id: str, inp: BoxRenderInputs) -> list:
    """Ordered (key, value) pairs for one service's env file. Secrets are ALWAYS
    ${VAR} refs (never plaintext)."""
    refs = inp.secret_refs
    product = _PRODUCT_OF[module_id]
    pairs: list = []
    if module_id in ("onebrain-api", "onebrain-workers"):
        pairs += [("ONEBRAIN_VECTOR_STORE", "pgvector"),
                  ("ONEBRAIN_DATABASE_URL", _db_url("onebrain")),
                  ("ONEBRAIN_DATA_DIR", "/data")]
    if module_id == "onebrain-api":
        pairs += [
            ("ONEBRAIN_DEPLOYMENT_ID", inp.deployment_id),
            ("ONEBRAIN_FLEET_URL", inp.fleet_url),
            (f"{refs.fleet_key_env}", "${" + refs.fleet_key_env + "}"),
            (f"{refs.llm_key_env}", "${" + refs.llm_key_env + "}"),
            (f"{refs.owner_bootstrap_env}", "${" + refs.owner_bootstrap_env + "}"),
            ("ONEBRAIN_MODULE_PROBES_ENABLED", "true"),
            ("ONEBRAIN_LOCAL_MODULES", ",".join(_ordered(inp.enabled_modules))),
        ]
    if module_id == "onebrain-admin-ui":
        pairs += [("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000")]
    if module_id == "assistant-service":
        pairs += [
            ("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000"),
            (refs.service_key_env, "${" + refs.service_key_env + "}"),
            ("DATABASE_URL", _db_url("assistant")),
            ("REDIS_URL", _redis_url()),
        ]
    if module_id in ("communication-api", "communication-workers"):
        pairs += [
            ("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000"),
            (refs.service_key_env, "${" + refs.service_key_env + "}"),
            (refs.space_id_env, "${" + refs.space_id_env + "}"),
        ]
        if module_id == "communication-api":
            pairs += [("ONEBRAIN_ACCOUNT_ID", inp.account_id)]
        pairs += [("DATABASE_URL", _db_url("communication")), ("REDIS_URL", _redis_url())]
    if module_id == "communication-voice":
        pairs += [("DATABASE_URL", _db_url("communication")), ("REDIS_URL", _redis_url())]
    if module_id == "communication-widget":
        pairs += [("ONEBRAIN_API_BASE_URL", "http://onebrain-api:8000")]
    if _is_http(module_id):
        pairs += [("TRUST_PROXY", str(inp.trust_proxy))]
    # A14 operator overlay (dormant in P4; only onebrain-api carries it).
    if module_id == "onebrain-api" and inp.role == "operator":
        pairs += [
            ("ONEBRAIN_IS_OPERATOR_SURFACE", "true"),
            ("ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS",
             "${ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS}"),
            ("ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY",
             "${ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY}"),
        ]
    return pairs


def _migrate_env(product: str) -> list:
    if product == "onebrain":
        return [("ONEBRAIN_VECTOR_STORE", "pgvector"),
                ("ONEBRAIN_DATABASE_URL", _db_url("onebrain")),
                ("ONEBRAIN_DATA_DIR", "/data")]
    return [("DATABASE_URL", _db_url(product))]


def render_env_files(inp: BoxRenderInputs) -> dict:
    _validate(inp)
    out: dict = {}
    # Infra env (secrets are ${VAR} refs).
    out["env/postgres.env"] = _kv([
        ("POSTGRES_USER", _PG_USER),
        ("POSTGRES_PASSWORD", "${" + inp.secret_refs.db_password_env + "}"),
        ("POSTGRES_INITDB_ARGS", "--auth-host=scram-sha-256"),
    ])
    out["env/redis.env"] = _kv([("REDIS_PASSWORD", "${" + inp.secret_refs.redis_password_env + "}")])
    for product in _enabled_products(inp.enabled_modules):
        if _migrate_included(inp, product):
            out[f"env/{product}-migrate.env"] = _kv(_migrate_env(product))
    for module_id in _ordered(inp.enabled_modules):
        out[f"env/{module_id}.env"] = _kv(_module_env(module_id, inp))
    return out


# --- Caddyfile ---------------------------------------------------------------
# publicly reverse-proxied HTTP modules -> (port, path matcher). Order matters
# (specific before the catch-all). Workers are internal (never public).
_CADDY_ROUTES = (
    ("onebrain-api", 8000, "/api/*"),
    ("onebrain-api", 8000, "/health*"),
    ("assistant-service", 8000, "/assistant/*"),
    ("communication-api", 4000, "/comm/api/*"),
    ("communication-widget", 5174, "/comm/widget/*"),
    ("communication-voice", 4100, "/comm/voice/*"),
)


def render_caddyfile(inp: BoxRenderInputs) -> str:
    _validate(inp)
    present = set(inp.enabled_modules)
    site = inp.fqdn if inp.fqdn else ":80"
    blocks = []
    for module_id, port, path in _CADDY_ROUTES:
        if module_id in present:
            blocks.append(f"    handle {path} {{\n        reverse_proxy {module_id}:{port}\n    }}")
    default = None
    if "onebrain-admin-ui" in present:
        default = ("onebrain-admin-ui", 3000)
    elif "onebrain-api" in present:
        default = ("onebrain-api", 8000)
    if default:
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


def _write_file_entry(path: str, content: str, permissions: str = "0644") -> str:
    return (
        f"  - path: {path}\n"
        f"    permissions: '{permissions}'\n"
        f"    content: |\n"
        f"{_yaml_block(content)}\n"
    )


def _yaml_sq(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _box_env(inp: BoxRenderInputs) -> str:
    products = " ".join(_enabled_products(inp.enabled_modules))
    pairs = [
        ("ONEBRAIN_FLEET_URL", inp.fleet_url),
        ("ONEBRAIN_DEPLOYMENT_ID", inp.deployment_id),
        ("ONEBRAIN_FLEET_KEY", "${" + inp.secret_refs.fleet_key_env + "}"),
        ("ONEBRAIN_RUN_ID", "{run_id}"),
        ("UPDATE_RELEASE_PUBLIC_KEY", inp.release_public_key),
        ("UPDATE_DESIRED_STATE_PUBLIC_KEY", inp.fleet_public_desired_state_key),
        ("UPDATE_REGISTRY_ALLOWLIST", inp.registry_allowlist),
        ("UPDATE_DATA_DIR", "/data"),
        ("UPDATE_COMPOSE_DIR", "/opt/onebrain"),
        ("UPDATE_COMPOSE_PROJECT", inp.compose_project),
        ("UPDATE_PROFILES", products),
        ("UPDATE_LOCAL_MODULES", ",".join(_ordered(inp.enabled_modules))),
        ("UPDATE_HEALTH_URL", "http://127.0.0.1/health"),
        # A5: the per-box backup-encryption key + the owner OTP + callback token
        # are ${VAR} refs; the host-OUTPUT metadata drop bounds their exposure.
        ("UPDATE_BACKUP_KEY", "${" + inp.secret_refs.backup_key_env + "}"),
        ("ONEBRAIN_ADMIN_PASSWORD", "${" + inp.secret_refs.owner_bootstrap_env + "}"),
        ("ONEBRAIN_PROVISIONING_CALLBACK_TOKEN", "${ONEBRAIN_PROVISIONING_CALLBACK_TOKEN}"),
    ]
    return _kv(pairs)


_META = "169.254.169.254"


def _callback_curl(status: str, smoke: str, *, extra: str = "") -> str:
    body = f'{{\\"status\\":\\"{status}\\",\\"smoke_status\\":\\"{smoke}\\"{extra}}}'
    return (
        "set -a; . /opt/onebrain/box.env; set +a; "
        'curl -sf -X POST -H "Authorization: Bearer ${ONEBRAIN_PROVISIONING_CALLBACK_TOKEN}" '
        '-H "Content-Type: application/json" '
        f'--data "{body}" '
        '"${ONEBRAIN_FLEET_URL}/api/provisioning/runs/{run_id}/callback"'
    )


def render_cloud_init(inp: BoxRenderInputs) -> str:
    _validate(inp)
    compose = render_compose(inp)
    caddy = render_caddyfile(inp)
    env_files = render_env_files(inp)

    entries = [_write_file_entry("/opt/onebrain/docker-compose.yml", compose)]
    for rel_path, content in env_files.items():
        entries.append(_write_file_entry(f"/opt/onebrain/{rel_path}", content))
    entries.append(_write_file_entry("/opt/onebrain/Caddyfile", caddy))
    entries.append(_write_file_entry("/opt/onebrain/box.env", _box_env(inp), "0600"))
    entries.append(_write_file_entry(
        "/opt/onebrain/postgres-init.sh", _read_box_file("postgres-init.sh"), "0755"))
    entries.append(_write_file_entry(
        "/opt/onebrain/update.sh", _read_box_file("update.sh"), "0755"))
    entries.append(_write_file_entry(
        "/opt/onebrain/onebrain_box_verify.py", _read_box_file("onebrain_box_verify.py"), "0644"))
    entries.append(_write_file_entry(
        "/etc/systemd/system/onebrain-update.service", _read_box_file("onebrain-update.service")))
    entries.append(_write_file_entry(
        "/etc/systemd/system/onebrain-update.timer", _read_box_file("onebrain-update.timer")))
    write_files = "".join(entries).rstrip("\n")

    profile_flags = " ".join(f"--profile {p}" for p in _enabled_products(inp.enabled_modules))
    compose_cmd = f"docker compose --project-name {inp.compose_project} {profile_flags}".strip()
    fail_cb = _callback_curl("failed", "failed", extra=',\\"failure_reason\\":\\"metadata_egress_block_failed\\"')
    done_cb = _callback_curl(
        "${ST}", "${SMOKE}",
        extra=',\\"bootstrap_password\\":\\"${ONEBRAIN_ADMIN_PASSWORD}\\"'
              ',\\"external_run_url\\":\\"$(cat /opt/onebrain/box.instance 2>/dev/null)\\"',
    )
    runcmd_items = [
        "mkdir -p /opt/onebrain/env /data /mnt/onebrain-data",
        # Mount the attached data volume so Postgres survives a rebuild (device id
        # is assigned by Hetzner; the real mount executes on the live box, P5).
        'for dev in /dev/disk/by-id/scsi-0HC_Volume_*; do [ -b "$dev" ] || continue; '
        'blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -F "$dev"; mount "$dev" /mnt/onebrain-data; done',
        "systemctl enable --now docker",
        # Capture the public IP for the callback BEFORE the metadata drop below.
        f"curl -sf http://{_META}/hetzner/v1/metadata/public-ipv4 > /opt/onebrain/box.instance 2>/dev/null || true",
        # A10: wait until dockerd has created the DOCKER-USER chain (bounded).
        "i=0; until iptables -L DOCKER-USER -n >/dev/null 2>&1; do i=$((i+1)); "
        '[ "$i" -ge 60 ] && break; sleep 1; done',
        # A5: BOTH the bridge (DOCKER-USER) and the host (OUTPUT) egress drops to
        # the metadata endpoint. A failed insert fails the boot and reports it.
        f"iptables -I DOCKER-USER -d {_META} -j DROP || {{ {fail_cb}; exit 1; }}",
        f"iptables -I OUTPUT -d {_META} -j DROP || {{ {fail_cb}; exit 1; }}",
        f"{compose_cmd} pull",
        f"{compose_cmd} up -d",
        "systemctl enable --now onebrain-update.timer",
        # Smoke + provisioning callback (bootstrap_password = the owner OTP).
        'sleep 5; if curl -sf http://127.0.0.1/health >/dev/null 2>&1; then ST=succeeded; SMOKE=passed; '
        f"else ST=failed; SMOKE=failed; fi; {done_cb} || true",
    ]
    runcmd = "\n".join("  - " + _yaml_sq(item) for item in runcmd_items)

    template = (_DEPLOY_TEMPLATES / "cloud-init.yaml.tmpl").read_text(encoding="utf-8")
    return template.replace("{{WRITE_FILES}}", write_files).replace("{{RUNCMD}}", runcmd)

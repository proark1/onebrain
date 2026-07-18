"""P4-02: the pure render layer (cloud-init + compose + Caddyfile + env). Golden
files for compose/Caddyfile; assertion-based for env + cloud-init. Regenerate the
goldens with ONEBRAIN_REGEN_GOLDEN=1 (documented here, used only when the intended
output changes)."""

from __future__ import annotations

import base64
import gzip
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from app.provisioning.hetzner.render import (
    BoxRenderInputs,
    render_caddyfile,
    render_cloud_init,
    render_compose,
    render_env_files,
)

# The Hetzner Cloud API rejects user_data over this many bytes (422 invalid_input); the
# rendered cloud-init MUST stay under it (the go-live blocker this file guards).
_HETZNER_USER_DATA_LIMIT = 32768


def _find_usable_bash() -> str | None:
    """Avoid treating an unconfigured Windows WSL launcher as Bash."""
    candidates = [shutil.which("bash")]
    # Git for Windows often exposes ``git`` but not ``bash`` on PATH. Its Bash
    # is nevertheless a real local shell, unlike the WSL launcher in System32.
    git = shutil.which("git")
    if git is not None:
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for candidate in dict.fromkeys(candidates):
        if candidate is None:
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


def _service_names(compose: str) -> set:
    """Top-level compose service names (2-space indent under `services:`) without a
    YAML parser (PyYAML is not a project dependency)."""
    names = set()
    in_services = False
    for line in compose.splitlines():
        if line == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            names.add(line.strip().rstrip(":"))
    return names


def _runcmd_section(cloud_init: str) -> str:
    """The small cloud-init launcher block only."""
    return cloud_init.split("\nruncmd:\n", 1)[1]


def _first_boot_section(cloud_init: str) -> str:
    """The extracted helper that contains the ordered first-boot commands."""
    return _asset_entries(cloud_init)["/opt/onebrain/onebrain-firstboot.sh"][1]


def _write_files_section(cloud_init: str) -> str:
    return cloud_init.split("\nruncmd:\n", 1)[0]


# A write_files entry emitted with cloud-init's `encoding: gz+b64` (large entries only):
#   - path: <path>
#     permissions: '<perm>'
#     encoding: gz+b64
#     content: <single-line base64 of gzip(original bytes)>
_GZB64_ENTRY = re.compile(
    r"^  - path: (?P<path>\S+)\n"
    r"    permissions: '(?P<perm>[0-7]+)'\n"
    r"    encoding: gz\+b64\n"
    r"    content: (?P<blob>\S+)\n",
    re.MULTILINE,
)

_XZB85_ENTRY = re.compile(
    r"^  - path: /root/ob\.b85\n"
    r"    permissions: '(?P<perm>[0-7]+)'\n"
    r"    content: \|\n"
    r"      (?P<blob>\S+)\n",
    re.MULTILINE,
)

_XZB64_ENTRY = re.compile(
    r"^  - path: (?P<path>\S+\.tar\.xz)\n"
    r"    permissions: '(?P<perm>[0-7]+)'\n"
    r"    encoding: b64\n"
    r"    content: (?P<blob>\S+)\n",
    re.MULTILINE,
)


def _gz_b64_raw_entries(cloud_init: str) -> dict:
    """{path: (permissions, decompressed_bytes)} for gz+b64 write_files entries."""
    out = {}
    for m in _GZB64_ENTRY.finditer(_write_files_section(cloud_init)):
        out[m.group("path")] = (m.group("perm"), gzip.decompress(base64.b64decode(m.group("blob"))))
    return out


def _asset_entries(cloud_init: str) -> dict:
    """Decode the deterministic non-secret asset tar written by cloud-init."""
    xz = _XZB85_ENTRY.search(_write_files_section(cloud_init))
    if xz is not None:
        perm = xz.group("perm")
        archive = lzma.decompress(base64.b85decode(xz.group("blob")))
    else:
        legacy_xz = _XZB64_ENTRY.search(_write_files_section(cloud_init))
        if legacy_xz is not None:
            perm = legacy_xz.group("perm")
            archive = lzma.decompress(base64.b64decode(legacy_xz.group("blob")))
        else:
            raw = _gz_b64_raw_entries(cloud_init)
            perm, archive = raw["/opt/onebrain/onebrain-assets.tar"]
    assert perm == "0600"
    out = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            assert handle is not None
            out["/" + member.name] = (format(member.mode, "04o"), handle.read().decode("utf-8"))
    return out


def _large_asset_entries(cloud_init: str) -> dict:
    """Large normal-asset members used by archive round-trip assertions.

    The primary archive is now XZ/Base85 rather than one gzip entry per asset;
    this helper deliberately describes the member size, not its transport
    encoding. The similarly named raw helper above remains for the MC secret
    archive, which intentionally stays gz+b64 for redaction compatibility.
    """
    return {
        path: item
        for path, item in _asset_entries(cloud_init).items()
        if len(item[1].encode("utf-8")) >= 1024
    }


_GOLDEN = Path(__file__).parent / "golden" / "hetzner"
_ALL = (
    "onebrain-api", "onebrain-admin-ui", "onebrain-workers", "assistant-service",
    "communication-api", "communication-widget", "communication-voice", "communication-workers",
)


def _digest(i: int) -> str:
    return "sha256:" + format(i, "064x")


def _images(modules) -> dict:
    return {m: f"ghcr.io/proark1/{m}@{_digest(idx)}" for idx, m in enumerate(_ALL) if m in set(modules)}


def _inputs(modules, *, fqdn="dep_a.fleet.example", role="customer", run_id="prun_fixture",
            bootstrap_token="", callback_token="", **over) -> BoxRenderInputs:
    base = dict(
        deployment_id="dep_a",
        account_id="acct_1",
        compose_project="onebrain-dep_a",
        enabled_modules=tuple(modules),
        images=_images(modules),
        fqdn=fqdn,
        fleet_url="https://mc.example.com",
        run_id=run_id,
        fleet_public_desired_state_key="DSPUBKEY",
        release_public_key="RELPUBKEY",
        release_version="2026.07.13.1",
        release_migration="0022_release_promotion_gate",
        module_versions={module_id: f"{module_id}-v1" for module_id in modules},
        registry_allowlist="ghcr.io/proark1",
        role=role,
        bootstrap_token=bootstrap_token,
        callback_token=callback_token,
    )
    base.update(over)                        # allow tests to override any field (e.g. backup_* for BK3)
    return BoxRenderInputs(**base)


def _assert_golden(name: str, actual: str) -> None:
    path = _GOLDEN / name
    if os.environ.get("ONEBRAIN_REGEN_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(actual.encode("utf-8"))
    assert path.exists(), f"missing golden {name}; regen with ONEBRAIN_REGEN_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")   # universal newlines -> LF
    assert actual == expected, f"golden mismatch for {name}; regen with ONEBRAIN_REGEN_GOLDEN=1"


_ONEBRAIN = ("onebrain-api", "onebrain-admin-ui", "onebrain-workers")


# --- compose -----------------------------------------------------------------
def test_compose_onebrain_only():
    compose = render_compose(_inputs(_ONEBRAIN))
    _assert_golden("compose_onebrain.yml", compose)
    services = _service_names(compose)
    assert "onebrain-migrate" in services
    assert "postgres-roles" in services
    assert "PGHOST=postgres exec /opt/onebrain/postgres-init.sh" in compose
    assert "      postgres-roles:\n        condition: service_completed_successfully" in compose
    assert "profiles: [onebrain]" in compose
    # one-shot migrate gates the api via service_completed_successfully
    assert "      onebrain-migrate:\n        condition: service_completed_successfully" in compose
    assert "- env/onebrain-api.env" in compose
    assert "- /data:/data" in compose
    assert compose.count("- /mnt/onebrain-data/drive:/data/drive") == 2
    assert "x-x: &x {read_only: true" in compose
    assert 'tmpfs: ["/tmp:mode=1777,size=64m", "/app/.next/cache:mode=1777,size=64m"]' in compose
    assert "edge: {ipv4_address: 172.30.0.2}" in compose
    assert "edge: {aliases: [api-edge]}" in compose
    assert "edge: {internal: true, ipam: {config: [{subnet: 172.30.0.0/24}]}}" in compose
    assert ":8080" not in compose                      # never Railway's masked port
    assert "8000" in compose and "3000" in compose     # onebrain ports
    for absent in ("4000", "5174", "4100", "4200"):
        assert absent not in compose                   # comm ports absent
    # postgres/redis/app services expose only; Caddy is the ONE ingress that publishes
    # host ports, and only 80/443 (the sole inbound path the Hetzner firewall allows).
    assert "expose:" in compose
    assert compose.count("ports:") == 1
    assert '"80:80"' in compose and '"443:443"' in compose
    assert "  caddy:\n    image: caddy:2@sha256:" in compose   # ingress present, no profile
    for support_image in (
        "caddy:2@sha256:844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9",
        "pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb",
        "redis:7@sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954",
    ):
        assert f"image: {support_image}" in compose


def test_compose_with_communication():
    modules = _ONEBRAIN + ("communication-api", "communication-widget", "communication-voice", "communication-workers")
    compose = render_compose(_inputs(modules))
    _assert_golden("compose_communication.yml", compose)
    assert "communication-migrate" in _service_names(compose)
    assert 'command: ["pnpm", "db:migrate"]' in compose
    for port in ("4000", "5174", "4100", "4200"):
        assert port in compose


def test_compose_with_assistant():
    modules = _ONEBRAIN + ("assistant-service",)
    compose = render_compose(_inputs(modules))
    _assert_golden("compose_assistant.yml", compose)
    assert "assistant-migrate" in _service_names(compose)
    # assistant-service depends on redis (service_healthy)
    assert "      redis:\n        condition: service_healthy" in compose


def test_compose_full_stack():
    compose = render_compose(_inputs(_ALL))
    _assert_golden("compose_full.yml", compose)
    services = _service_names(compose)
    # images map covers exactly the enabled modules (+ ingress + infra + migrates)
    module_services = {s for s in services if s not in ("caddy", "postgres", "postgres-roles", "redis") and not s.endswith("-migrate")}
    assert module_services == set(_ALL)
    # Caddy has NO profile, so the ingress is present on every stack regardless of products
    assert "caddy" in services


def test_full_stack_accepts_one_digest_pinned_communication_image_for_all_modules():
    """The communication release intentionally uses a single multi-service image."""
    images = _images(_ALL)
    shared = f"ghcr.io/proark1/communication@{_digest(42)}"
    for module_id in ("communication-api", "communication-widget",
                      "communication-voice", "communication-workers"):
        images[module_id] = shared
    compose = render_compose(_inputs(_ALL, images=images))
    # Four long-running services plus the communication migration job use the
    # same immutable image; SERVICE in each env file selects the process.
    assert compose.count(f"image: {shared}") == 5


# --- env files ---------------------------------------------------------------
_SECRET_KEYS = (
    "POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "POSTGRES_WORKER_PASSWORD",
    "POSTGRES_ASSISTANT_PASSWORD", "POSTGRES_COMMUNICATION_PASSWORD", "REDIS_PASSWORD",
    "ONEBRAIN_LLM_API_KEY", "ONEBRAIN_SERVICE_KEY", "ONEBRAIN_ADMIN_PASSWORD",
    "ONEBRAIN_ASSISTANT_SERVICE_KEY", "ONEBRAIN_COMMUNICATION_SERVICE_KEY",
    "ONEBRAIN_AUTH_SECRET", "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",
)


def test_env_files_are_per_service_and_cover_requirements():
    from app.provisioning.bundles import OPTIONAL_MODULE_IDS
    from app.provisioning.customer_bootstrap import CustomerBootstrapDescriptor, encode_customer_bootstrap

    descriptor = encode_customer_bootstrap(CustomerBootstrapDescriptor(
        account_id="acct_1", account_kind="organization", customer_name="Customer A",
        module_ids=OPTIONAL_MODULE_IDS,
    ))
    inp = _inputs(_ALL, customer_bootstrap=descriptor)
    env = render_env_files(inp)
    # one file per enabled service (+ infra + migrates)
    assert "env/communication-api.env" in env
    comm = env["env/communication-api.env"]
    assert "ONEBRAIN_SERVICE_KEY=" in comm and "ONEBRAIN_SPACE_ID=" in comm   # via MODULE_ENV_REQUIREMENTS
    assert "ONEBRAIN_SERVICE_KEY=${ONEBRAIN_COMMUNICATION_SERVICE_KEY}" in comm
    assert "ONEBRAIN_SPACE_ID=${ONEBRAIN_COMMUNICATION_SPACE_ID}" in comm
    assistant = env["env/assistant-service.env"]
    assert "ONEBRAIN_SERVICE_KEY=${ONEBRAIN_ASSISTANT_SERVICE_KEY}" in assistant
    api = env["env/onebrain-api.env"]
    assert f"ONEBRAIN_CUSTOMER_BOOTSTRAP={descriptor}" in api
    assert "ONEBRAIN_ASSISTANT_SERVICE_KEY=${ONEBRAIN_ASSISTANT_SERVICE_KEY}" in api
    assert "ONEBRAIN_COMMUNICATION_SERVICE_KEY=${ONEBRAIN_COMMUNICATION_SERVICE_KEY}" in api
    assert "ONEBRAIN_MODULE_PROBES_ENABLED=true" in api
    assert "ONEBRAIN_LOCAL_MODULES=" in api
    assert "ONEBRAIN_DATA_DIR=/data" in api
    assert "TRUST_PROXY" not in api
    # The admin seed pair — seed.py (seed_admin_from_env) creates a loginable admin at
    # container start ONLY when BOTH are non-empty. Both are ${VAR} refs filled from the
    # exchanged (customer) / baked (MC) /opt/onebrain/.env; without the email the box seeds
    # no admin and — SSH closed — is unreachable.
    assert "ONEBRAIN_ADMIN_EMAIL=${ONEBRAIN_ADMIN_EMAIL}" in api
    assert "ONEBRAIN_ADMIN_PASSWORD=${ONEBRAIN_ADMIN_PASSWORD}" in api
    # The customer-facing application never receives the fleet credential. The
    # root-only host update/reporter agent reads it from box.env instead.
    assert "ONEBRAIN_FLEET_KEY" not in api
    assert "ONEBRAIN_FLEET_URL" not in api
    # Inert for modules that do NOT seed (only onebrain-api runs seed_admin_from_env).
    for non_seeding in ("env/onebrain-workers.env", "env/communication-api.env",
                        "env/assistant-service.env", "env/onebrain-admin-ui.env"):
        assert "ONEBRAIN_ADMIN_EMAIL" not in env[non_seeding]
    # No plaintext secret: every secret key is a ${VAR} ref; DB/redis URLs embed refs.
    for content in env.values():
        for line in content.splitlines():
            key, _, value = line.partition("=")
            if key in _SECRET_KEYS:
                if key == "ONEBRAIN_SERVICE_KEY":
                    assert value in {
                        "${ONEBRAIN_ASSISTANT_SERVICE_KEY}",
                        "${ONEBRAIN_COMMUNICATION_SERVICE_KEY}",
                    }
                else:
                    assert value == "${" + key + "}", f"{key} is not a ${{VAR}} ref: {line!r}"
            if key in ("ONEBRAIN_DATABASE_URL", "DATABASE_URL", "ONEBRAIN_WORKER_DATABASE_URL"):
                assert "${POSTGRES_" in value             # password is a ref, never plaintext
                assert "_URLENCODED}" in value             # URL component is safely encoded at dotenv render time
            if key == "REDIS_URL":
                assert "${REDIS_PASSWORD_URLENCODED}" in value


def test_shared_communication_image_services_receive_explicit_selectors():
    """The communication repository dispatches its one image from SERVICE.

    A full-stack manifest can pin the same immutable image under four module
    IDs, but each Compose service must still start the matching process.
    """
    env = render_env_files(_inputs(_ALL))
    expected = {
        "communication-api": "api",
        "communication-widget": "widget",
        "communication-voice": "voice",
        "communication-workers": "workers",
    }
    for module_id, selector in expected.items():
        assert f"SERVICE={selector}" in env[f"env/{module_id}.env"]
    for module_id in _ONEBRAIN + ("assistant-service",):
        assert "SERVICE=" not in env[f"env/{module_id}.env"]


def test_env_bakes_production_boot_essentials():
    env = render_env_files(_inputs(_ALL))
    api = env["env/onebrain-api.env"]
    workers = env["env/onebrain-workers.env"]
    # ONEBRAIN_ENVIRONMENT=production arms is_production_like -> validate_runtime_safety's net;
    # ONEBRAIN_RLS_ENFORCED=true enforces tenant isolation. Both belong on the api AND the
    # worker (they share the tenant Postgres).
    for content in (api, workers):
        assert "ONEBRAIN_ENVIRONMENT=production" in content
        assert "ONEBRAIN_RLS_ENFORCED=true" in content
        assert "ONEBRAIN_VECTOR_STORE=pgvector" in content
        assert "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET=${ONEBRAIN_LOGIN_RATE_LIMIT_SECRET}" in content
    # The cookie secret (a ${VAR} ref) + Secure cookies live ONLY on onebrain-api — the worker
    # never constructs the app / signs cookies, so it neither validates nor needs the secret.
    assert "ONEBRAIN_AUTH_SECRET=${ONEBRAIN_AUTH_SECRET}" in api
    assert "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET=${ONEBRAIN_LOGIN_RATE_LIMIT_SECRET}" in api
    assert "ONEBRAIN_COOKIE_SECURE=true" in api
    assert "ONEBRAIN_TRUSTED_PROXY_CIDRS=172.30.0.2/32" in api
    assert "ONEBRAIN_TRUSTED_PROXY_HOPS=1" in api
    assert "ONEBRAIN_POSTGRES_APP_ROLE=onebrain_app" in api
    assert "ONEBRAIN_POSTGRES_WORKER_ROLE=onebrain_worker" in api
    assert "postgresql://onebrain_app:${POSTGRES_APP_PASSWORD_URLENCODED}@postgres:5432/onebrain" in api
    assert "ONEBRAIN_WORKER_DATABASE_URL" not in api
    assert "ONEBRAIN_WORKER_DATABASE_URL=postgresql://onebrain_worker:${POSTGRES_WORKER_PASSWORD_URLENCODED}@postgres:5432/onebrain" in workers
    assert "postgresql://assistant_app:${POSTGRES_ASSISTANT_PASSWORD_URLENCODED}@postgres:5432/assistant" in env["env/assistant-service.env"]
    assert "postgresql://communication_app:${POSTGRES_COMMUNICATION_PASSWORD_URLENCODED}@postgres:5432/communication" in env["env/communication-api.env"]
    assert "ONEBRAIN_AUTH_SECRET" not in workers
    assert "ONEBRAIN_COOKIE_SECURE" not in workers


def test_per_product_databases_are_distinct():
    env = render_env_files(_inputs(_ALL))
    assert env["env/onebrain-api.env"].count("@postgres:5432/onebrain\n") == 1
    assert "@postgres:5432/assistant" in env["env/assistant-service.env"]
    assert "@postgres:5432/communication" in env["env/communication-api.env"]
    # the two independent alembic lineages target DISTINCT databases (no shared alembic_version)
    ob_db = "postgresql://onebrain:${POSTGRES_PASSWORD_URLENCODED}@postgres:5432/onebrain"
    as_db = "postgresql://onebrain:${POSTGRES_PASSWORD_URLENCODED}@postgres:5432/assistant"
    assert ob_db in env["env/onebrain-migrate.env"]
    assert as_db in env["env/assistant-migrate.env"]
    assert ob_db != as_db
    assert "ONEBRAIN_DATABASE_URL=postgresql://onebrain_app:${POSTGRES_APP_PASSWORD_URLENCODED}@postgres:5432/onebrain" in env["env/onebrain-api.env"]
    assert "ONEBRAIN_PROCESS=worker" in env["env/onebrain-workers.env"]
    assert "ONEBRAIN_WORKER_DATABASE_URL=postgresql://onebrain_worker:${POSTGRES_WORKER_PASSWORD_URLENCODED}@postgres:5432/onebrain" in env["env/onebrain-workers.env"]
    # Runtime services never receive the product owner's global credential;
    # the owner DSNs remain only in their one-shot migration environments.
    for name in ("env/assistant-service.env", "env/communication-api.env",
                 "env/communication-workers.env", "env/communication-voice.env"):
        assert "postgresql://onebrain:${POSTGRES_PASSWORD_URLENCODED}" not in env[name]
    # the createdb names equal the Phase-6 pg_restore targets
    from app.provisioning.hetzner import render as R
    init = R._read_box_file("postgres-init.sh")
    for db in ("onebrain", "assistant", "communication"):
        assert db in init
    for role in ("POSTGRES_ASSISTANT_ROLE", "POSTGRES_COMMUNICATION_ROLE"):
        assert role in init
    assert "ALTER DEFAULT PRIVILEGES" in init


def test_runtime_database_roles_must_be_distinct_non_owner_logins():
    with pytest.raises(ValueError, match="must all differ"):
        render_compose(_inputs(_ONEBRAIN, postgres_assistant_role="onebrain_app"))
    with pytest.raises(ValueError, match="must not use the owner"):
        render_compose(_inputs(_ONEBRAIN, postgres_assistant_role="onebrain"))


def test_render_operator_overlay():
    op = render_env_files(_inputs(_ALL, role="operator"))["env/onebrain-api.env"]
    # The settable field that actually arms Mission Control (is_operator_surface is a
    # read-only @property, so the surface flag alone leaves operator_mode False).
    assert "ONEBRAIN_OPERATOR_MODE=true" in op
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in op
    assert "ONEBRAIN_FLEET_PUBLIC_URL=https://mc.example.com" in op   # MC's own public URL
    assert "ONEBRAIN_FLEET_URL=https://mc.example.com" in op        # self-pointing (caller sets the URL)
    assert "ONEBRAIN_OPERATOR_DATABASE_URL=postgresql://onebrain:${POSTGRES_PASSWORD_URLENCODED}@postgres:5432/onebrain" in op
    assert "ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS=" in op
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=${ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY}" in op
    # G1-1: the box's OWN accepted wrapper-key set, or its startup assertion bricks it.
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS=${ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS}" in op
    cust = render_env_files(_inputs(_ALL, role="customer"))["env/onebrain-api.env"]
    # Customer application containers explicitly fail closed: they do not merely
    # rely on framework defaults to hide the control plane.
    assert "ONEBRAIN_OPERATOR_MODE=false" in cust
    assert "ONEBRAIN_OPERATOR_CONSOLE=false" in cust
    assert "ONEBRAIN_FLEET_REPORTER_ENABLED=false" in cust
    assert "ONEBRAIN_IS_OPERATOR_SURFACE" not in cust
    assert "ONEBRAIN_FLEET_URL" not in cust
    assert "ONEBRAIN_FLEET_KEY" not in cust
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY" not in cust
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS" not in cust
    assert "ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS" not in cust


def test_operator_render_rejects_customer_bootstrap_descriptor():
    from app.provisioning.bundles import OPTIONAL_MODULE_IDS
    from app.provisioning.customer_bootstrap import CustomerBootstrapDescriptor, encode_customer_bootstrap

    descriptor = encode_customer_bootstrap(CustomerBootstrapDescriptor(
        account_id="acct_1", account_kind="organization", customer_name="Customer A",
        module_ids=OPTIONAL_MODULE_IDS,
    ))
    with pytest.raises(ValueError, match="customer_bootstrap is only valid"):
        render_env_files(_inputs(_ALL, role="operator", customer_bootstrap=descriptor))


# --- BK3: offsite-backup config delivery -------------------------------------
def test_box_env_bakes_backup_config_off_by_default():
    from app.provisioning.hetzner.render import _box_env
    be = _box_env(_inputs(_ONEBRAIN))
    assert "ONEBRAIN_BACKUP_ENABLED=false" in be                    # the gate is ALWAYS baked
    assert "ONEBRAIN_GATE_AGENT_ENABLED=true" in be                 # customer host only
    assert "UPDATE_ROLE_SPLIT_REQUIRED=" not in be                  # update.sh defaults the successor fence on
    assert "UPDATE_INITIAL_RELEASE_FILE=/opt/onebrain/installed-release.json" in be
    assert "ONEBRAIN_MAINTENANCE_DIR=/mnt/onebrain-data/onebrain-maintenance" in be
    assert "ONEBRAIN_DATA_MOUNT=" not in be                         # host-script default is the verified mount
    assert "ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT=" not in be          # host-script default is sibling verifier
    # an INERT box (backups off, the default) carries NO other backup config -> zero box.env bloat
    assert "ONEBRAIN_BACKUP_S3_ENDPOINT" not in be
    assert "ONEBRAIN_BACKUP_S3_ACCESS_KEY" not in be
    assert "ONEBRAIN_BACKUP_DBS" not in be


def test_box_env_bakes_backup_config_when_enabled():
    from app.provisioning.hetzner.render import _box_env
    be = _box_env(_inputs(
        _ALL, backup_enabled=True, backup_s3_endpoint="https://fsn1.your-objectstorage.com",
        backup_s3_bucket="ob-backups", backup_s3_region="fsn1",
        backup_dbs=("onebrain", "assistant", "communication")))
    assert "ONEBRAIN_BACKUP_ENABLED=true" in be
    assert "ONEBRAIN_BACKUP_S3_ENDPOINT=https://fsn1.your-objectstorage.com" in be
    assert "ONEBRAIN_BACKUP_S3_BUCKET=ob-backups" in be
    assert "ONEBRAIN_BACKUP_S3_REGION=fsn1" in be
    assert "ONEBRAIN_BACKUP_DBS=onebrain assistant communication" in be
    # the two S3 credentials are ${VAR} refs — NEVER literal secrets baked into box.env
    assert "ONEBRAIN_BACKUP_S3_ACCESS_KEY=${ONEBRAIN_BACKUP_S3_ACCESS_KEY}" in be
    assert "ONEBRAIN_BACKUP_S3_SECRET_KEY=${ONEBRAIN_BACKUP_S3_SECRET_KEY}" in be


@pytest.mark.parametrize(
    ("fqdn", "expected_health_url"),
    [
        ("dep_a.fleet.example", "https://dep_a.fleet.example/health"),
        ("", "http://127.0.0.1/health"),
    ],
)
def test_box_health_probes_follow_fqdn_when_tls_is_enabled(fqdn, expected_health_url):
    """The updater and first-boot smoke probe the same reachable endpoint."""
    from app.provisioning.hetzner.render import _box_env

    inp = _inputs(_ONEBRAIN, fqdn=fqdn)

    assert f"UPDATE_HEALTH_URL={expected_health_url}" in _box_env(inp)
    assert f"curl -sf {expected_health_url}" in _first_boot_section(render_cloud_init(inp))


def test_enabled_product_dbs_tracks_products():
    from app.provisioning.hetzner.render import enabled_product_dbs
    assert enabled_product_dbs(_ONEBRAIN) == ("onebrain",)
    assert enabled_product_dbs(_ALL) == ("onebrain", "assistant", "communication")


def test_backup_endpoint_eu_allowlist_fails_closed():
    from app.config import Settings
    # disabled -> no check at all
    Settings(backup_enabled=False,
             backup_object_store_endpoint="https://s3.amazonaws.com").assert_backup_endpoint_eu()
    # enabled + approved EU host (bare host AND a bucket subdomain) -> OK
    Settings(backup_enabled=True,
             backup_object_store_endpoint="https://fsn1.your-objectstorage.com").assert_backup_endpoint_eu()
    Settings(backup_enabled=True,
             backup_object_store_endpoint="https://ob.fsn1.your-objectstorage.com").assert_backup_endpoint_eu()
    # enabled + non-EU host -> fail closed (never exfiltrate EU personal data offshore)
    with pytest.raises(ValueError, match="not an approved EU endpoint"):
        Settings(backup_enabled=True,
                 backup_object_store_endpoint="https://s3.us-east-1.amazonaws.com").assert_backup_endpoint_eu()
    # a look-alike that only loosely SUFFIX-matches (no dot boundary) must NOT pass
    with pytest.raises(ValueError):
        Settings(backup_enabled=True,
                 backup_object_store_endpoint="https://evil-fsn1.your-objectstorage.com").assert_backup_endpoint_eu()


def test_backup_object_store_configured_property():
    from app.config import Settings
    assert Settings().backup_object_store_configured is False
    full = Settings(backup_object_store_endpoint="https://fsn1.your-objectstorage.com",
                    backup_object_store_bucket="b", backup_object_store_access_key="AK",
                    backup_object_store_secret_key="SK")
    assert full.backup_object_store_configured is True


# --- Caddyfile ---------------------------------------------------------------
def test_caddyfile_routes_only_enabled_and_tls():
    _assert_golden("caddy_full.yml", render_caddyfile(_inputs(_ALL)))
    http_only = render_caddyfile(_inputs(_ONEBRAIN, fqdn=""))
    _assert_golden("caddy_http_only.yml", http_only)
    assert http_only.startswith(":80 {")                 # fqdn="" -> http-only on the IP
    full = render_caddyfile(_inputs(_ALL))
    assert full.startswith("dep_a.fleet.example {")      # a domain implies Caddy auto-HTTPS (80/443)
    assert "reverse_proxy communication-api:4000" in full
    assert "dynamic a api-edge 8000" in full
    assert "refresh 5s" in full
    assert "header_up X-Forwarded-For {remote_host}" in full
    assert "uri replace /api/onebrain /api" in full
    # Denials come before generic /api/* routing so a customer browser cannot
    # even reach an unmounted control-plane route through the API proxy.
    deny = 'handle /api/fleet/* {\n        respond "Not Found" 404\n    }'
    assert deny in full
    assert full.index(deny) < full.index("handle /api/*")
    for path in ("/api/operator/*", "/api/provisioning/*", "/api/rollouts/*"):
        assert f'handle {path} {{\n        respond "Not Found" 404\n    }}' in full
    for path in ("/api/fleet", "/api/operator", "/api/provisioning", "/api/rollouts"):
        assert f'handle {path} {{\n        respond "Not Found" 404\n    }}' in full


def test_initial_release_descriptor_is_metadata_only_and_complete():
    import json
    from app.provisioning.hetzner.render import _initial_release_descriptor

    descriptor = json.loads(_initial_release_descriptor(_inputs(_ALL)))
    assert descriptor == {
        "version": "2026.07.13.1",
        "migration_to": "0022_release_promotion_gate",
        "modules": {module_id: f"{module_id}-v1" for module_id in _ALL},
    }


# --- cloud-init --------------------------------------------------------------
def test_cloud_init_embeds_all_artifacts_and_egress_block():
    ci = render_cloud_init(_inputs(_ALL))
    assert "\n  - python3\n" in ci  # Base85 archive decoder is stdlib Python.
    assert "- python3-cryptography" in ci
    wf = _write_files_section(ci)
    assert "- path: /root/ob.b85" in wf
    assets = _asset_entries(ci)
    for required in (
        "/opt/onebrain/docker-compose.yml", "/opt/onebrain/Caddyfile", "/opt/onebrain/box.env",
        "/opt/onebrain/postgres-init.sh", "/opt/onebrain/onebrain_dotenv.sh", "/opt/onebrain/update.sh",
        "/opt/onebrain/onebrain-data-volume.sh",
        "/opt/onebrain/onebrain_box_verify.py", "/opt/onebrain/onebrain-gate-agent.sh",
        "/opt/onebrain/onebrain_gate_report.py", "/opt/onebrain/onebrain-firstboot.sh",
        "/opt/onebrain/installed-release.json",
        "/etc/onebrain-drive-enabled", "/etc/systemd/system/onebrain-data-volume.service",
        "/etc/systemd/system/onebrain-drive-backup.service",
        "/etc/systemd/system/onebrain-drive-backup.timer",
        "/etc/systemd/system/onebrain-drive-erasure-ledger.service",
        "/etc/systemd/system/onebrain-drive-erasure-ledger.timer",
        "/etc/systemd/system/onebrain-update.service", "/etc/systemd/system/onebrain-update.timer",
    ):
        assert required in assets
    assert "/opt/onebrain/env/onebrain-api.env" in assets
    assert "set -euo pipefail" in assets["/opt/onebrain/update.sh"][1]
    assert assets["/opt/onebrain/onebrain_dotenv.sh"][0] == "0644"
    assert "onebrain_load_dotenv()" in assets["/opt/onebrain/onebrain_dotenv.sh"][1]
    assert "verify_desired_state" in assets["/opt/onebrain/onebrain_box_verify.py"][1]
    assert "set -euo pipefail" not in wf                             # NOT present as plaintext (compressed)
    assert "ExecStart=/opt/onebrain/onebrain-gate-agent.sh" in assets["/etc/systemd/system/onebrain-update.service"][1]
    # Both metadata DROP rules (A5) and the callback live in the extracted
    # first-boot helper, while cloud-init only launches it.
    first_boot = _first_boot_section(ci)
    assert "iptables -w -I DOCKER-USER -d 169.254.169.254 -j DROP" in first_boot
    assert "iptables -w -I OUTPUT -d 169.254.169.254 -j DROP" in first_boot
    # The default target comes from trusted box.env; a preflighted custom URL is
    # single-quoted at the cloud-init call site so ordinary URL syntax is safe.
    callback = assets["/opt/onebrain/onebrain-gate-agent.sh"][1]
    assert "/api/provisioning/runs/${ONEBRAIN_RUN_ID:-}/callback" in callback
    assert "ONEBRAIN_RUN_ID=prun_fixture" in assets["/opt/onebrain/box.env"][1]
    assert "{run_id}" not in callback
    assert assets["/opt/onebrain/onebrain_gate_report.py"][0] == "0755"
    assert "base64.b85decode" in _runcmd_section(ci)
    assert "tar -xJf - -C /" in _runcmd_section(ci)
    assert "bash /opt/onebrain/onebrain-firstboot.sh" in _runcmd_section(ci)


def test_cloud_init_installs_volume_contract_and_safe_host_maintenance():
    """Every host pins its data mount before Docker; cleanup retains rollback images."""
    ci = render_cloud_init(_inputs(_ONEBRAIN))
    assets = _asset_entries(ci)
    for path, perm in (
        ("/opt/onebrain/onebrain-data-volume.sh", "0755"),
        ("/etc/systemd/system/onebrain-data-volume.service", "0644"),
        ("/opt/onebrain/onebrain-host-maintenance.sh", "0755"),
        ("/etc/systemd/system/onebrain-host-maintenance.service", "0644"),
        ("/etc/systemd/system/onebrain-host-maintenance.timer", "0644"),
        ("/opt/onebrain/onebrain-postgres-collation.sh", "0755"),
    ):
        assert assets[path][0] == perm

    volume_unit = assets["/etc/systemd/system/onebrain-data-volume.service"][1]
    assert "Before=docker.service" in volume_unit
    assert "RequiredBy=docker.service" in volume_unit
    maintenance_unit = assets["/etc/systemd/system/onebrain-host-maintenance.service"][1]
    assert "User=root" in maintenance_unit
    assert "Nice=19" in maintenance_unit
    assert "IOSchedulingClass=idle" in maintenance_unit
    maintenance = assets["/opt/onebrain/onebrain-host-maintenance.sh"][1]
    assert "images.override.yml" in maintenance
    assert "images.override.prev.yml" in maintenance
    assert "last_applied.json" in maintenance
    assert "docker image prune" not in maintenance

    first_boot = _first_boot_section(ci)
    stop_docker = first_boot.index("systemctl stop docker.service docker.socket")
    volume_setup = first_boot.index("onebrain-data-volume.sh setup")
    maintenance_dir = first_boot.index("install -d -o root -g root -m 0700 /mnt/onebrain-data/onebrain-maintenance")
    volume_enable = first_boot.index("systemctl enable onebrain-data-volume.service")
    docker_enable = first_boot.index("systemctl enable --now docker")
    compose_up = first_boot.index("up -d")
    assert stop_docker < volume_setup < maintenance_dir < volume_enable < docker_enable < compose_up
    assert "chown -Rh 10001:10001 /mnt/onebrain-data" not in first_boot
    assert "systemctl enable --now onebrain-host-maintenance.timer" in first_boot


def test_customer_drive_volume_is_persistent_verified_and_ordered_before_docker():
    ci = render_cloud_init(_inputs(_ONEBRAIN))
    assets = _asset_entries(ci)
    first_boot = _first_boot_section(ci)
    setup = first_boot.index("onebrain-data-volume.sh setup")
    docker = first_boot.index("systemctl enable --now docker")

    assert first_boot.index("systemctl stop docker.service docker.socket") < setup < docker
    assert first_boot.index("systemctl enable onebrain-data-volume.service") < docker
    assert "systemctl enable --now onebrain-drive-backup.timer" in first_boot
    ledger_init = first_boot.index("systemctl start onebrain-drive-erasure-ledger.service")
    ledger_timer = first_boot.index("systemctl enable --now onebrain-drive-erasure-ledger.timer")
    backup_timer = first_boot.index("systemctl enable --now onebrain-drive-backup.timer")
    assert first_boot.index("up -d") < ledger_init < ledger_timer < backup_timer

    volume_script = assets["/opt/onebrain/onebrain-data-volume.sh"][1]
    volume_unit = assets["/etc/systemd/system/onebrain-data-volume.service"][1]
    backup_unit = assets["/etc/systemd/system/onebrain-drive-backup.service"][1]
    assert "UUID=$uuid $DATA_MOUNT ext4" in volume_script
    assert ">>/etc/fstab" in volume_script
    assert 'mountpoint -q "$DATA_MOUNT"' in volume_script
    assert "mounted filesystem UUID does not match" in volume_script
    assert 'install -d -o 10001 -g 10001 -m 0750 "$DRIVE_DIR"' in volume_script
    assert "RequiresMountsFor=/mnt/onebrain-data" in volume_unit
    assert "Before=docker.service" in volume_unit
    assert "RequiredBy=docker.service" in volume_unit
    assert "{{COMPOSE_PROJECT}}" not in backup_unit
    assert "--project-name onebrain-dep_a" in backup_unit
    assert "onebrain-api:/app/deploy/box/onebrain-drive-backup.sh" in backup_unit
    assert "onebrain_backup_crypto.py" in backup_unit
    assert "onebrain_erasure_ledger.py" in backup_unit


def test_operator_receives_no_drive_mount_or_backup_surface():
    inp = _inputs(_ONEBRAIN, role="operator")
    compose = render_compose(inp)
    ci = render_cloud_init(inp)
    assets = _asset_entries(ci)
    first_boot = _first_boot_section(ci)

    assert "/mnt/onebrain-data/drive:/data/drive" not in compose
    assert "/etc/onebrain-drive-enabled" not in assets
    assert "/etc/systemd/system/onebrain-drive-backup.service" not in assets
    assert "/etc/systemd/system/onebrain-drive-backup.timer" not in assets
    assert "/etc/systemd/system/onebrain-drive-erasure-ledger.service" not in assets
    assert "/etc/systemd/system/onebrain-drive-erasure-ledger.timer" not in assets
    assert "onebrain-drive-backup.timer" not in first_boot
    assert "onebrain-drive-erasure-ledger" not in first_boot
    # Current main mounts and verifies the host data volume for every role;
    # operators omit only the Drive-specific mount and lifecycle surfaces.
    assert "/opt/onebrain/onebrain-data-volume.sh" in assets
    assert "/etc/systemd/system/onebrain-data-volume.service" in assets
    assert "scsi-0HC_Volume_*" in assets["/opt/onebrain/onebrain-data-volume.sh"][1]


def test_cloud_init_uses_the_preflighted_callback_url_template():
    ci = render_cloud_init(_inputs(
        _ONEBRAIN,
        callback_url="https://callbacks.example/provisioning/runs/{run_id}/callback?source=box&channel=agent",
    ))
    first_boot = _first_boot_section(ci)
    assert "ONEBRAIN_CALLBACK_URL=" in first_boot
    assert "https://callbacks.example/provisioning/runs/prun_fixture/callback?source=box&channel=agent" in first_boot


def test_cloud_init_requires_run_id():
    with pytest.raises(ValueError, match="run_id is required"):
        render_cloud_init(_inputs(_ONEBRAIN, run_id=""))


def test_cloud_init_compose_calls_are_anchored():
    """First boot: the extracted helper runs with cwd '/', so every `docker compose`
    invocation must carry `-f /opt/onebrain/docker-compose.yml` or Compose V2 finds no
    file and the box never starts (matches update.sh's dc() wrapper)."""
    first_boot = _first_boot_section(render_cloud_init(_inputs(_ALL)))
    compose_lines = [ln for ln in first_boot.splitlines() if "docker compose" in ln]
    assert compose_lines, "expected docker compose calls in first-boot helper"
    for ln in compose_lines:
        assert "-f /opt/onebrain/docker-compose.yml" in ln, f"unanchored compose call: {ln!r}"


def test_cloud_init_metadata_block_is_fail_soft_before_compose_up():
    """Box-boot robustness (fix/box-boot-robustness): the metadata-egress DROP is defense in
    depth (inbound is already firewalled; the onebrain-metadata-drop.service is the
    authoritative drop), so a transient in-memory insert failure must NOT brick the box. The
    first-boot metadata-drop lines FAIL SOFT — no `exit 1` — so `docker compose ... up -d` ALWAYS
    runs after them and the box serves; the failure callback is still POSTed (operator signal)."""
    ci = render_cloud_init(_inputs(_ONEBRAIN))
    first_boot = _first_boot_section(ci)
    guard = first_boot.index("iptables -L DOCKER-USER")
    drop_du = first_boot.index("iptables -w -I DOCKER-USER -d 169.254.169.254 -j DROP")
    drop_out = first_boot.index("iptables -w -I OUTPUT -d 169.254.169.254 -j DROP")
    persist = first_boot.index("systemctl enable --now onebrain-metadata-drop.service")
    compose_up = first_boot.index("up -d")
    # A10 ordering preserved: wait-for-chain -> both drops -> persist/apply now -> compose up.
    assert guard < drop_du < drop_out < persist < compose_up
    # `docker compose ... up -d` is present AND strictly AFTER the metadata step (never gated
    # behind it / never skipped when a drop insert fails).
    assert drop_out < first_boot.index("docker compose") < compose_up
    # FAIL SOFT: the metadata-drop lines no longer abort the boot with `exit 1`; a failed
    # insert reports-and-continues (`|| true`) so cloud-init proceeds to compose up.
    assert "exit 1" not in first_boot
    for start in (drop_du, drop_out):
        line = first_boot[start:first_boot.index("\n", start)]
        assert "exit 1" not in line
        assert "|| true" in line          # report-and-continue, not fail-closed
    # The failure callback (operator signal) is STILL emitted on an insert failure;
    # its fixed reason is JSON-encoded by the root-only reporter callback mode.
    assert 'ONEBRAIN_CALLBACK_KIND="failure"' in first_boot
    assert "metadata_egress_block_failed" in _asset_entries(ci)["/opt/onebrain/onebrain_gate_report.py"][1]


# --- Hetzner 32768-byte user_data limit (compact XZ asset archive) ------------
def test_customer_cloud_init_under_hetzner_user_data_limit():
    """The go-live blocker: Hetzner's Cloud API rejects user_data over 32768 bytes. The
    full-stack customer box (every module) is the largest customer render and MUST fit,
    with headroom below the hard limit."""
    for modules in (_ALL, _ONEBRAIN):
        n = len(render_cloud_init(_inputs(modules)).encode("utf-8"))
        assert n < _HETZNER_USER_DATA_LIMIT, (
            f"customer cloud-init {n} bytes exceeds Hetzner's {_HETZNER_USER_DATA_LIMIT}-byte limit")
    # Keep a deliberate payload budget for normal source growth. A full stack
    # must retain at least 512 bytes below Hetzner's fixed API boundary.
    assert len(render_cloud_init(_inputs(_ALL)).encode("utf-8")) < 32256
    # The onebrain-only box (what the MC box mirrors module-wise) retains a
    # concrete 768-byte safety margin after the extra runtime-role isolation
    # configuration is rendered.
    assert len(render_cloud_init(_inputs(_ONEBRAIN)).encode("utf-8")) < 32000


def test_cloud_init_compact_xz_archive_roundtrips_safe_host_assets():
    """The normal host-tool archive is deterministic XZ/Base85 and retains executable assets.

    The renderer removes only safe standalone source comments from the archive
    copy; every other byte is preserved. Operator secrets stay in a separate
    opaque gzip archive so bootstrap dry-runs can still redact it as one unit.
    """
    from app.provisioning.hetzner import render as R

    inp = _inputs(_ALL)
    ci = render_cloud_init(inp)
    assets = _asset_entries(ci)

    # The full Compose file stays byte-identical. Executable source uses the
    # deliberate compact form, while all non-comment content remains unchanged.
    expected = {
        "/opt/onebrain/docker-compose.yml": (render_compose(inp), "0644"),
        "/opt/onebrain/update.sh": (
            R._compact_host_asset("/opt/onebrain/update.sh", R._read_box_file("update.sh")), "0755"
        ),
        "/opt/onebrain/onebrain_bootstrap.sh": (
            R._compact_host_asset("/opt/onebrain/onebrain_bootstrap.sh", R._read_box_file("onebrain_bootstrap.sh")),
            "0755",
        ),
        "/opt/onebrain/onebrain_box_verify.py": (
            R._compact_host_asset("/opt/onebrain/onebrain_box_verify.py", R._read_box_file("onebrain_box_verify.py")),
            "0644",
        ),
        "/etc/systemd/system/onebrain-metadata-drop.service": (
            R._compact_host_asset(
                "/etc/systemd/system/onebrain-metadata-drop.service",
                R._read_box_file("onebrain-metadata-drop.service"),
            ),
            "0644",
        ),
    }
    for path, (original, want_perm) in expected.items():
        assert path in assets, f"{path} should remain in the compact archive"
        got_perm, decoded = assets[path]
        assert decoded == original, f"{path}: compact archive changed executable content"
        assert got_perm == want_perm, f"{path}: permission {got_perm!r} not preserved (want {want_perm!r})"
    # The executable shell tools stay executable after cloud-init extracts the tar.
    assert assets["/opt/onebrain/update.sh"][0] == "0755"
    assert assets["/opt/onebrain/onebrain_bootstrap.sh"][0] == "0755"

    # Customer box.env is inside the root-only archive to fit Hetzner's hard
    # user-data limit. Its short-lived tokens remain mode 0600 after extraction.
    assert assets["/opt/onebrain/box.env"][0] == "0600"
    assert "/opt/onebrain/box.env" not in _write_files_section(ci)

    assert _XZB85_ENTRY.search(_write_files_section(ci))

    # Deterministic/reproducible: XZ has no clock field -> identical render.
    assert render_cloud_init(inp) == ci


def test_compact_host_assets_extract_as_valid_executable_source():
    """The size compactor cannot alter script data, shebangs, or syntax."""
    from app.provisioning.hetzner import render as R

    assets = _asset_entries(render_cloud_init(_inputs(_ALL)))
    source_assets = {
        "/opt/onebrain/postgres-init.sh": "postgres-init.sh",
        "/opt/onebrain/onebrain_dotenv.sh": "onebrain_dotenv.sh",
        "/opt/onebrain/update.sh": "update.sh",
        "/opt/onebrain/onebrain-gate-agent.sh": "onebrain-gate-agent.sh",
        "/opt/onebrain/onebrain_gate_report.py": "onebrain_gate_report.py",
        "/opt/onebrain/onebrain_box_verify.py": "onebrain_box_verify.py",
        "/opt/onebrain/onebrain-data-volume.sh": "onebrain-data-volume.sh",
        "/opt/onebrain/onebrain-host-maintenance.sh": "onebrain-host-maintenance.sh",
        "/opt/onebrain/onebrain-postgres-collation.sh": "onebrain-postgres-collation.sh",
        "/opt/onebrain/onebrain_bootstrap.sh": "onebrain_bootstrap.sh",
        "/etc/systemd/system/onebrain-data-volume.service": "onebrain-data-volume.service",
        "/etc/systemd/system/onebrain-update.service": "onebrain-update.service",
        "/etc/systemd/system/onebrain-update.timer": "onebrain-update.timer",
        "/etc/systemd/system/onebrain-host-maintenance.service": "onebrain-host-maintenance.service",
        "/etc/systemd/system/onebrain-host-maintenance.timer": "onebrain-host-maintenance.timer",
        "/etc/systemd/system/onebrain-metadata-drop.service": "onebrain-metadata-drop.service",
    }
    for path, filename in source_assets.items():
        source = R._read_box_file(filename)
        assert assets[path][1] == R._compact_host_asset(path, source)

    for path in ("/opt/onebrain/onebrain_gate_report.py", "/opt/onebrain/onebrain_box_verify.py"):
        compile(assets[path][1], path, "exec")

    bash = _find_usable_bash()
    if bash is None:
        pytest.skip("functional bash unavailable for extracted-script syntax check")
    for path in (*source_assets, "/opt/onebrain/onebrain-firstboot.sh"):
        if not path.endswith(".sh"):
            continue
        text = assets[path][1]
        assert text.startswith("#!"), f"{path}: compact archive lost its shebang"
        result = subprocess.run([bash, "-n"], input=text, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"{path}: {result.stderr}"


def test_compact_shell_asset_preserves_heredoc_data_comments():
    from app.provisioning.hetzner import render as R

    source = "#!/usr/bin/env bash\n# shell comment\ncat <<'DATA'\n# data comment\nDATA\n# trailing\n"
    assert R._compact_host_asset("/opt/example.sh", source) == (
        "#!/usr/bin/env bash\ncat <<'DATA'\n# data comment\nDATA\n"
    )


def test_compact_host_assets_drop_only_safe_blank_lines():
    from app.provisioning.hetzner import render as R

    shell = "#!/usr/bin/env bash\n\necho before\ncat <<'DATA'\n\nDATA\n\necho after\n"
    assert R._compact_host_asset("/opt/example.sh", shell) == (
        "#!/usr/bin/env bash\necho before\ncat <<'DATA'\n\nDATA\necho after\n"
    )
    python = 'value = """first\n\nsecond"""\n\nprint(value)\n'
    compacted_python = R._compact_host_asset("/opt/example.py", python)
    namespace: dict[str, object] = {}
    exec(compacted_python, namespace)
    assert namespace["value"] == "first\n\nsecond"


def test_mc_cloud_init_under_hetzner_user_data_limit():
    """The MC (operator) box — the actual go-live artifact bootstrap_mc renders — fits under
    Hetzner's 32768-byte user_data limit via build_mc_artifacts. Its baked dotenv and
    optional broker mTLS material live in a dedicated opaque archive, which dry-runs redact
    as a unit while boot-config tests still resolve its original contents."""
    from tests.test_bootstrap_mc import _args, _base_argv, _mc_settings, mc

    art = mc.build_mc_artifacts(_args(_base_argv()), _mc_settings())
    n = len(art.server.user_data.encode("utf-8"))
    assert n < _HETZNER_USER_DATA_LIMIT, (
        f"MC user_data {n} bytes exceeds Hetzner's {_HETZNER_USER_DATA_LIMIT}-byte limit")
    assert n < 32000
    gz = _large_asset_entries(art.server.user_data)
    assert "/opt/onebrain/onebrain_box_verify.py" in gz     # the large verifier is compressed
    assert "/opt/onebrain/.env" not in gz                   # restored only after MC-secret archive extraction
    assert "/opt/onebrain/mc-broker-tls.tar" in _gz_b64_raw_entries(art.server.user_data)


def test_update_sh_has_no_crlf():
    # A1: a CRLF checkout would break Cygwin/Git-Bash on `set -euo pipefail\r`.
    from app.provisioning.hetzner import render as R
    raw = (R._DEPLOY_BOX / "update.sh").read_bytes()
    assert b"\r" not in raw


# --- P5-03 bootstrap exchange + G1-6 metadata-drop persistence ---------------
def test_box_env_bakes_bootstrap_and_callback_tokens_only():
    from app.provisioning.hetzner.render import _box_env
    be = _box_env(_inputs(_ALL, bootstrap_token="bt_id_sec", callback_token="cbtok"))
    # G1-7: the callback token is BAKED (a real value), NOT a ${VAR} ref, so fail_cb
    # authenticates before the exchange.
    assert "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN=cbtok" in be
    assert "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN=${" not in be
    # P5-03: the single-use first-boot token is baked for a customer box.
    assert "ONEBRAIN_BOOTSTRAP_TOKEN=bt_id_sec" in be
    # Every OTHER secret stays a ${VAR} ref filled by the exchange (never plaintext).
    for line in be.splitlines():
        key, _, value = line.partition("=")
        if key in ("ONEBRAIN_FLEET_KEY", "UPDATE_BACKUP_KEY", "ONEBRAIN_ADMIN_PASSWORD"):
            assert value == "${" + key + "}", f"{key} should stay a ref: {line!r}"


def test_operator_box_env_omits_bootstrap_token():
    # G3-1: the MC box (role=operator) bakes its .env directly and runs no exchange, so
    # it is never given a bootstrap token; the callback token is still baked.
    from app.provisioning.hetzner.render import _box_env
    be = _box_env(_inputs(_ALL, role="operator", callback_token="cbtok"))
    assert "ONEBRAIN_BOOTSTRAP_TOKEN" not in be
    assert "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN=cbtok" in be


def test_cloud_init_embeds_bootstrap_helper_and_metadata_drop_unit():
    ci = render_cloud_init(_inputs(_ALL, bootstrap_token="bt_x_y", callback_token="cb"))
    assets = _asset_entries(ci)
    assert "/opt/onebrain/onebrain_bootstrap.sh" in assets
    # G1-6: the boot-persistent metadata-egress DROP oneshot is embedded.
    assert "/etc/systemd/system/onebrain-metadata-drop.service" in assets


def test_cloud_init_bootstrap_runcmd_order_and_literal_env_first_load():
    ci = render_cloud_init(_inputs(_ALL, bootstrap_token="bt_x_y", callback_token="cb"))
    first_boot = _first_boot_section(ci)
    # Order: immediate DROP -> persist across reboots (G1-6) -> secret exchange -> compose up.
    drop = first_boot.index("iptables -w -I OUTPUT -d 169.254.169.254 -j DROP")
    persist = first_boot.index("systemctl enable --now onebrain-metadata-drop.service")
    exchange = first_boot.index("bash /opt/onebrain/onebrain_bootstrap.sh")
    up = first_boot.index("up -d")
    assert drop < persist < exchange < up
    # The gate agent's callback mode literal-loads .env before renderer-owned
    # box.env expands its ${VAR} references; it must never source exchanged values as code.
    assert "/opt/onebrain/onebrain-gate-agent.sh --provision-callback" in first_boot
    callback = _asset_entries(ci)["/opt/onebrain/onebrain-gate-agent.sh"][1]
    helper = callback.index('. "$DOTENV_LOADER"')
    dotenv = callback.index('onebrain_load_dotenv "$ENV_FILE"')
    box_env = callback.index('. "$BOX_ENV"')
    assert helper < dotenv < box_env
    assert '. "$ENV_FILE"' not in callback
    assert '"$GATE_REPORTER" --provision-callback' in callback
    reporter = _asset_entries(ci)["/opt/onebrain/onebrain_gate_report.py"][1]
    assert "json.dumps" in reporter


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None or shutil.which("python3") is None,
    reason="callback command runtime test requires POSIX sh and python3",
)
def test_callback_json_encodes_literal_password_with_quotes_and_backslashes(tmp_path):
    from app.provisioning.hetzner.render import _read_box_file

    root = tmp_path / "onebrain"
    root.mkdir()
    password = 'new"password\\with\\slashes'
    (root / "onebrain_dotenv.sh").write_text(_read_box_file("onebrain_dotenv.sh"), encoding="utf-8")
    callback = root / "onebrain-gate-agent.sh"
    callback.write_text(_read_box_file("onebrain-gate-agent.sh"), encoding="utf-8")
    callback.chmod(0o755)
    reporter = root / "onebrain_gate_report.py"
    reporter.write_text(_read_box_file("onebrain_gate_report.py"), encoding="utf-8")
    reporter.chmod(0o755)
    (root / ".env").write_text(f"ONEBRAIN_ADMIN_PASSWORD={password}\n", encoding="utf-8")
    (root / "box.env").write_text(
        "ONEBRAIN_FLEET_URL=https://mc.test\n"
        "ONEBRAIN_RUN_ID=prun_fixture\n"
        "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN=callback-token\n"
        "ONEBRAIN_ADMIN_PASSWORD=${ONEBRAIN_ADMIN_PASSWORD}\n",
        encoding="utf-8",
    )
    callback_url = "https://callbacks.test/provisioning/prun_fixture?source=box&channel=agent"
    (root / "box.instance").write_text("https://dep.test\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body_path = tmp_path / "callback.json"
    args_path = tmp_path / "callback.args"
    curl = bin_dir / "curl"
    curl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CALLBACK_ARGS"\ncat > "$CALLBACK_BODY"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALLBACK_BODY": str(body_path),
        "CALLBACK_ARGS": str(args_path),
        "ONEBRAIN_CALLBACK_URL": callback_url,
        "ONEBRAIN_CALLBACK_STATUS": "succeeded",
        "ONEBRAIN_CALLBACK_SMOKE": "passed",
        "ONEBRAIN_CALLBACK_KIND": "completion",
    }

    result = subprocess.run([str(callback), "--provision-callback"], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(body_path.read_text(encoding="utf-8")) == {
        "status": "succeeded",
        "smoke_status": "passed",
        "bootstrap_password": password,
        "external_run_url": "https://dep.test",
    }
    assert args_path.read_text(encoding="utf-8").splitlines()[-1] == callback_url


def test_operator_cloud_init_omits_exchange_but_keeps_drop_persistence():
    # G3-1: the MC box render carries NO exchange step (it bakes .env), but G1-6's
    # metadata-drop persistence still applies to it.
    ci = render_cloud_init(_inputs(_ALL, role="operator"))
    first_boot = _first_boot_section(ci)
    assert "onebrain_bootstrap.sh" not in first_boot
    assert "systemctl enable --now onebrain-metadata-drop.service" in first_boot
    assets = _asset_entries(ci)
    assert "/opt/onebrain/onebrain_bootstrap.sh" not in assets
    # The MC has no provisioning-run callback target. It reports readiness via
    # its in-app self-heartbeat, so it does not carry the customer host reporter.
    assert "/opt/onebrain/onebrain_gate_report.py" not in assets
    assert "--provision-callback" not in first_boot


def test_metadata_drop_unit_runs_before_docker_and_precreates_chain():
    # G1-6 reboot race: the DOCKER-USER drop must be in place BEFORE dockerd starts any
    # restart:always container. An After=docker unit races the async container start, so the
    # unit is ordered Before=docker.service and CREATES the DOCKER-USER chain when it does not
    # yet exist (pre-docker boot) instead of skipping the container-path drop.
    from app.provisioning.hetzner import render as R
    unit = R._read_box_file("onebrain-metadata-drop.service")
    assert "Before=docker.service" in unit
    assert "After=docker.service" not in unit               # no longer races container start
    assert "iptables -N DOCKER-USER" in unit                # pre-create the chain (fallback)
    assert "-I DOCKER-USER -d 169.254.169.254 -j DROP" in unit
    assert "-I OUTPUT -d 169.254.169.254 -j DROP" in unit


def test_render_rejects_hostile_token():
    with pytest.raises(ValueError, match="bootstrap_token"):
        render_cloud_init(_inputs(_ONEBRAIN, bootstrap_token="bt; rm -rf /"))
    with pytest.raises(ValueError, match="callback_token"):
        render_cloud_init(_inputs(_ONEBRAIN, callback_token="a b"))
    with pytest.raises(ValueError, match="callback_url"):
        render_cloud_init(_inputs(
            _ONEBRAIN,
            callback_url="https://callbacks.example/x?run=$(id)&id={run_id}",
        ))


# --- injection discipline ----------------------------------------------------
def test_render_rejects_hostile_ids():
    with pytest.raises(ValueError):
        render_compose(BoxRenderInputs(
            deployment_id="x; rm -rf /", account_id="a", compose_project="onebrain-x",
            enabled_modules=_ONEBRAIN, images=_images(_ONEBRAIN)))
    # a floating-tag (non-digest) image ref is rejected
    bad_images = dict(_images(_ONEBRAIN))
    bad_images["onebrain-api"] = "ghcr.io/proark1/onebrain-api:latest"
    with pytest.raises(ValueError):
        render_compose(BoxRenderInputs(deployment_id="dep_a", account_id="a", compose_project="onebrain-dep_a",
                                       enabled_modules=_ONEBRAIN, images=bad_images))


def test_images_map_must_cover_enabled_modules():
    images = _images(_ONEBRAIN)
    del images["onebrain-workers"]
    with pytest.raises(ValueError):
        render_compose(BoxRenderInputs(deployment_id="dep_a", account_id="a", compose_project="onebrain-dep_a",
                                       enabled_modules=_ONEBRAIN, images=images))

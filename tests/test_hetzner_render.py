"""P4-02: the pure render layer (cloud-init + compose + Caddyfile + env). Golden
files for compose/Caddyfile; assertion-based for env + cloud-init. Regenerate the
goldens with ONEBRAIN_REGEN_GOLDEN=1 (documented here, used only when the intended
output changes)."""

from __future__ import annotations

import base64
import gzip
import io
import os
import re
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


def _service_names(compose: str) -> set:
    """Top-level compose service names (2-space indent under `services:`) without a
    YAML parser (PyYAML is not a project dependency)."""
    names = set()
    for line in compose.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            names.add(line.strip().rstrip(":"))
    return names


def _runcmd_section(cloud_init: str) -> str:
    """The runcmd block only (isolated from write_files, which embeds files whose
    text would otherwise collide with runcmd substrings like 'up -d')."""
    return cloud_init.split("\nruncmd:\n", 1)[1]


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


def _gz_b64_raw_entries(cloud_init: str) -> dict:
    """{path: (permissions, decompressed_bytes)} for gz+b64 write_files entries."""
    out = {}
    for m in _GZB64_ENTRY.finditer(_write_files_section(cloud_init)):
        out[m.group("path")] = (m.group("perm"), gzip.decompress(base64.b64decode(m.group("blob"))))
    return out


def _asset_entries(cloud_init: str) -> dict:
    """Decode the deterministic non-secret asset tar written by cloud-init."""
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


def _gz_b64_entries(cloud_init: str) -> dict:
    """Compatibility view of archive members that would individually benefit
    from gzip. Tests use this for the large-script round-trip assertions."""
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
    assert "profiles: [onebrain]" in compose
    # one-shot migrate gates the api via service_completed_successfully
    assert "      onebrain-migrate:\n        condition: service_completed_successfully" in compose
    assert "- env/onebrain-api.env" in compose
    assert "- /data:/data" in compose
    assert ":8080" not in compose                      # never Railway's masked port
    assert "8000" in compose and "3000" in compose     # onebrain ports
    for absent in ("4000", "5174", "4100", "4200"):
        assert absent not in compose                   # comm ports absent
    # postgres/redis/app services expose only; Caddy is the ONE ingress that publishes
    # host ports, and only 80/443 (the sole inbound path the Hetzner firewall allows).
    assert "expose:" in compose
    assert compose.count("ports:") == 1
    assert '"80:80"' in compose and '"443:443"' in compose
    assert "  caddy:\n    image: caddy:2" in compose   # ingress present, no profile


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
    module_services = {s for s in services if s not in ("caddy", "postgres", "redis") and not s.endswith("-migrate")}
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
    "POSTGRES_PASSWORD", "REDIS_PASSWORD",
    "ONEBRAIN_LLM_API_KEY", "ONEBRAIN_SERVICE_KEY", "ONEBRAIN_ADMIN_PASSWORD",
    "ONEBRAIN_AUTH_SECRET",
)


def test_env_files_are_per_service_and_cover_requirements():
    inp = _inputs(_ALL)
    env = render_env_files(inp)
    # one file per enabled service (+ infra + migrates)
    assert "env/communication-api.env" in env
    comm = env["env/communication-api.env"]
    assert "ONEBRAIN_SERVICE_KEY=" in comm and "ONEBRAIN_SPACE_ID=" in comm   # via MODULE_ENV_REQUIREMENTS
    api = env["env/onebrain-api.env"]
    assert "ONEBRAIN_MODULE_PROBES_ENABLED=true" in api
    assert "ONEBRAIN_LOCAL_MODULES=" in api
    assert "ONEBRAIN_DATA_DIR=/data" in api
    assert "TRUST_PROXY=1" in api
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
                assert value == "${" + key + "}", f"{key} is not a ${{VAR}} ref: {line!r}"
            if key in ("ONEBRAIN_DATABASE_URL", "DATABASE_URL"):
                assert "${POSTGRES_PASSWORD}" in value    # password is a ref, never plaintext
            if key == "REDIS_URL":
                assert "${REDIS_PASSWORD}" in value


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
    # The cookie secret (a ${VAR} ref) + Secure cookies live ONLY on onebrain-api — the worker
    # never constructs the app / signs cookies, so it neither validates nor needs the secret.
    assert "ONEBRAIN_AUTH_SECRET=${ONEBRAIN_AUTH_SECRET}" in api
    assert "ONEBRAIN_COOKIE_SECURE=true" in api
    assert "ONEBRAIN_AUTH_SECRET" not in workers
    assert "ONEBRAIN_COOKIE_SECURE" not in workers


def test_per_product_databases_are_distinct():
    env = render_env_files(_inputs(_ALL))
    assert env["env/onebrain-api.env"].count("@postgres:5432/onebrain\n") == 1
    assert "@postgres:5432/assistant" in env["env/assistant-service.env"]
    assert "@postgres:5432/communication" in env["env/communication-api.env"]
    # the two independent alembic lineages target DISTINCT databases (no shared alembic_version)
    ob_db = "postgresql://onebrain:${POSTGRES_PASSWORD}@postgres:5432/onebrain"
    as_db = "postgresql://onebrain:${POSTGRES_PASSWORD}@postgres:5432/assistant"
    assert ob_db in env["env/onebrain-migrate.env"]
    assert as_db in env["env/assistant-migrate.env"]
    assert ob_db != as_db
    # the createdb names equal the Phase-6 pg_restore targets
    from app.provisioning.hetzner import render as R
    init = R._read_box_file("postgres-init.sh")
    for db in ("onebrain", "assistant", "communication"):
        assert db in init


def test_render_operator_overlay():
    op = render_env_files(_inputs(_ALL, role="operator"))["env/onebrain-api.env"]
    # The settable field that actually arms Mission Control (is_operator_surface is a
    # read-only @property, so the surface flag alone leaves operator_mode False).
    assert "ONEBRAIN_OPERATOR_MODE=true" in op
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in op
    assert "ONEBRAIN_FLEET_PUBLIC_URL=https://mc.example.com" in op   # MC's own public URL
    assert "ONEBRAIN_FLEET_URL=https://mc.example.com" in op        # self-pointing (caller sets the URL)
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


# --- BK3: offsite-backup config delivery -------------------------------------
def test_box_env_bakes_backup_config_off_by_default():
    from app.provisioning.hetzner.render import _box_env
    be = _box_env(_inputs(_ONEBRAIN))
    assert "ONEBRAIN_BACKUP_ENABLED=false" in be                    # the gate is ALWAYS baked
    assert "ONEBRAIN_GATE_AGENT_ENABLED=true" in be                 # customer host only
    assert "UPDATE_INITIAL_RELEASE_FILE=/opt/onebrain/installed-release.json" in be
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
    assert "reverse_proxy onebrain-api:8000" in full
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
    assert "- python3-cryptography" in ci
    wf = _write_files_section(ci)
    assert "- path: /opt/onebrain/onebrain-assets.tar" in wf
    assets = _asset_entries(ci)
    for required in (
        "/opt/onebrain/docker-compose.yml", "/opt/onebrain/Caddyfile", "/opt/onebrain/box.env",
        "/opt/onebrain/postgres-init.sh", "/opt/onebrain/update.sh",
        "/opt/onebrain/onebrain_box_verify.py", "/opt/onebrain/onebrain-gate-agent.sh",
        "/opt/onebrain/onebrain_gate_report.py", "/opt/onebrain/installed-release.json",
        "/etc/systemd/system/onebrain-update.service", "/etc/systemd/system/onebrain-update.timer",
    ):
        if required == "/opt/onebrain/box.env":
            assert f"- path: {required}" in wf
        else:
            assert required in assets
    assert "/opt/onebrain/env/onebrain-api.env" in assets
    assert "set -euo pipefail" in assets["/opt/onebrain/update.sh"][1]
    assert "verify_desired_state" in assets["/opt/onebrain/onebrain_box_verify.py"][1]
    assert "set -euo pipefail" not in wf                             # NOT present as plaintext (compressed)
    assert "ExecStart=/opt/onebrain/onebrain-gate-agent.sh" in assets["/etc/systemd/system/onebrain-update.service"][1]
    # both metadata DROP rules (A5, in runcmd) + the run-id-substituted callback
    rc = _runcmd_section(ci)
    assert "iptables -w -I DOCKER-USER -d 169.254.169.254 -j DROP" in rc
    assert "iptables -w -I OUTPUT -d 169.254.169.254 -j DROP" in rc
    # run_id is baked at render time (no literal placeholder survives), in both the
    # callback URL and box.env — else the box POSTs to /runs/{run_id}/callback and 404s.
    assert "/api/provisioning/runs/prun_fixture/callback" in ci
    assert "ONEBRAIN_RUN_ID=prun_fixture" in ci
    assert "{run_id}" not in ci
    assert assets["/opt/onebrain/onebrain_gate_report.py"][0] == "0755"
    assert "tar -xf /opt/onebrain/onebrain-assets.tar -C /" in rc


def test_cloud_init_requires_run_id():
    with pytest.raises(ValueError, match="run_id is required"):
        render_cloud_init(_inputs(_ONEBRAIN, run_id=""))


def test_cloud_init_compose_calls_are_anchored():
    """First boot: cloud-init runcmd runs with cwd '/', so every `docker compose`
    invocation must carry `-f /opt/onebrain/docker-compose.yml` or Compose V2 finds no
    file and the box never starts (matches update.sh's dc() wrapper)."""
    runcmd = _runcmd_section(render_cloud_init(_inputs(_ALL)))
    compose_lines = [ln for ln in runcmd.splitlines() if "docker compose" in ln]
    assert compose_lines, "expected docker compose calls in runcmd"
    for ln in compose_lines:
        assert "-f /opt/onebrain/docker-compose.yml" in ln, f"unanchored compose call: {ln!r}"


def test_cloud_init_metadata_block_is_fail_soft_before_compose_up():
    """Box-boot robustness (fix/box-boot-robustness): the metadata-egress DROP is defense in
    depth (inbound is already firewalled; the onebrain-metadata-drop.service is the
    authoritative drop), so a transient in-memory insert failure must NOT brick the box. The
    runcmd metadata-drop lines FAIL SOFT — no `exit 1` — so `docker compose ... up -d` ALWAYS
    runs after them and the box serves; the failure callback is still POSTed (operator signal)."""
    ci = render_cloud_init(_inputs(_ONEBRAIN))
    runcmd = _runcmd_section(ci)   # isolate runcmd; embedded files also contain "up -d"
    guard = runcmd.index("iptables -L DOCKER-USER")
    drop_du = runcmd.index("iptables -w -I DOCKER-USER -d 169.254.169.254 -j DROP")
    drop_out = runcmd.index("iptables -w -I OUTPUT -d 169.254.169.254 -j DROP")
    persist = runcmd.index("systemctl enable --now onebrain-metadata-drop.service")
    compose_up = runcmd.index("up -d")
    # A10 ordering preserved: wait-for-chain -> both drops -> persist/apply now -> compose up.
    assert guard < drop_du < drop_out < persist < compose_up
    # `docker compose ... up -d` is present AND strictly AFTER the metadata step (never gated
    # behind it / never skipped when a drop insert fails).
    assert drop_out < runcmd.index("docker compose") < compose_up
    # FAIL SOFT: the metadata-drop lines no longer abort the boot with `exit 1`; a failed
    # insert reports-and-continues (`|| true`) so cloud-init proceeds to compose up.
    assert "exit 1" not in runcmd
    for start in (drop_du, drop_out):
        line = runcmd[start:runcmd.index("\n", start)]
        assert "exit 1" not in line
        assert "|| true" in line          # report-and-continue, not fail-closed
    # The failure callback (operator signal) is STILL emitted on an insert failure.
    assert "metadata_egress_block_failed" in runcmd


# --- Hetzner 32768-byte user_data limit (gz+b64 large-entry encoding) ---------
def test_customer_cloud_init_under_hetzner_user_data_limit():
    """The go-live blocker: Hetzner's Cloud API rejects user_data over 32768 bytes. The
    full-stack customer box (every module) is the largest customer render and MUST fit,
    with headroom below the hard limit."""
    for modules in (_ALL, _ONEBRAIN):
        n = len(render_cloud_init(_inputs(modules)).encode("utf-8"))
        assert n < _HETZNER_USER_DATA_LIMIT, (
            f"customer cloud-init {n} bytes exceeds Hetzner's {_HETZNER_USER_DATA_LIMIT}-byte limit")
    # The onebrain-only box (what the MC box mirrors module-wise) has a comfortable margin.
    assert len(render_cloud_init(_inputs(_ONEBRAIN)).encode("utf-8")) < 30000


def test_cloud_init_large_entries_are_gz_b64_and_roundtrip_byte_identical():
    """Compressible write_files entries (the box scripts, the full compose, the non-secret
    systemd units) are embedded with cloud-init's `encoding: gz+b64` WHEN that is smaller than
    the plain form, and base64-decode + gunzip reproduces the ORIGINAL bytes EXACTLY (cloud-init
    writes those decompressed bytes to disk), with permissions preserved. The SECRET-bearing
    entries (env/*.env, box.env, the MC-baked .env) are FORCED plain so bootstrap_mc._redact can
    mask their ${VAR}s / baked values and the boot-config tests can resolve them."""
    from app.provisioning.hetzner import render as R

    inp = _inputs(_ALL)
    ci = render_cloud_init(inp)
    gz = _gz_b64_entries(ci)

    # The large box scripts + the full compose are always gz+b64 and round-trip byte-identical.
    # The ~1.8KB metadata-drop systemd unit now compresses too — the main user_data reclaim that
    # keeps a FULL-STACK box comfortably under Hetzner's 32768-byte limit (was ~700B of headroom).
    expected = {
        "/opt/onebrain/docker-compose.yml": (render_compose(inp), "0644"),
        "/opt/onebrain/update.sh": (R._read_box_file("update.sh"), "0755"),
        "/opt/onebrain/onebrain_bootstrap.sh": (R._read_box_file("onebrain_bootstrap.sh"), "0755"),
        "/opt/onebrain/onebrain_box_verify.py": (R._read_box_file("onebrain_box_verify.py"), "0644"),
        "/etc/systemd/system/onebrain-metadata-drop.service":
            (R._read_box_file("onebrain-metadata-drop.service"), "0644"),
    }
    for path, (original, want_perm) in expected.items():
        assert path in gz, f"{path} should be gz+b64 (it is strictly smaller than plain)"
        got_perm, decoded = gz[path]
        assert decoded == original, f"{path}: gz+b64 round-trip is not byte-identical to the original"
        assert got_perm == want_perm, f"{path}: permission {got_perm!r} not preserved (want {want_perm!r})"
    # The two shell box scripts stay executable (0755) — cloud-init applies these perms to the
    # DECOMPRESSED file, so the box scripts remain runnable exactly as before.
    assert gz["/opt/onebrain/update.sh"][0] == "0755"
    assert gz["/opt/onebrain/onebrain_bootstrap.sh"][0] == "0755"

    # SECURITY: every SECRET-bearing entry stays plain (never gz+b64) so _redact can mask its
    # values and the boot-config tests can resolve them. env/*.env + box.env for a customer box
    # (a full-stack box has no baked .env; the MC .env plain-ness is covered in the MC test).
    wf = _write_files_section(ci)
    secret_plain = ["/opt/onebrain/box.env"]
    for plain_path in secret_plain:
        assert plain_path not in gz, f"{plain_path} was unexpectedly gz+b64 (a secret must stay plain)"
        body = wf.split(f"  - path: {plain_path}\n", 1)[1].split("  - path:", 1)[0]
        assert "content: |" in body and "encoding:" not in body, f"{plain_path} is not a plain entry"

    # Every gz+b64 entry is a genuine WIN: re-emitting its decoded content as forced-plain is
    # LONGER than the chosen gz+b64 entry (pick-smaller is never a pessimization).
    for path, (perm, decoded) in gz.items():
        plain_entry = R._write_file_entry(path, decoded, perm, compressible=False)
        gz_entry = R._write_file_entry(path, decoded, perm, compressible=True)
        assert "encoding: gz+b64" in gz_entry and len(gz_entry) < len(plain_entry), \
            f"{path}: gz+b64 is not smaller than plain"

    # Deterministic/reproducible: gzip mtime=0 (+ platform-independent OS byte) -> identical render.
    assert render_cloud_init(inp) == ci


def test_mc_cloud_init_under_hetzner_user_data_limit():
    """The MC (operator) box — the actual go-live artifact bootstrap_mc renders — fits under
    Hetzner's 32768-byte user_data limit via build_mc_artifacts, with a comfortable margin,
    while its baked /opt/onebrain/.env stays PLAIN (so bootstrap_mc._redact can mask its
    secret values and the boot-config tests can resolve it)."""
    from tests.test_bootstrap_mc import _args, _base_argv, _mc_settings, mc

    art = mc.build_mc_artifacts(_args(_base_argv()), _mc_settings())
    n = len(art.server.user_data.encode("utf-8"))
    assert n < _HETZNER_USER_DATA_LIMIT, (
        f"MC user_data {n} bytes exceeds Hetzner's {_HETZNER_USER_DATA_LIMIT}-byte limit")
    assert n < 30000, f"MC user_data {n} bytes has no comfortable margin under {_HETZNER_USER_DATA_LIMIT}"
    gz = _gz_b64_entries(art.server.user_data)
    assert "/opt/onebrain/onebrain_box_verify.py" in gz     # the large verifier is compressed
    assert "/opt/onebrain/.env" not in gz                   # the baked .env stays plain (redaction/tests)


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


def test_cloud_init_bootstrap_runcmd_order_and_env_first_source():
    rc = _runcmd_section(render_cloud_init(_inputs(_ALL, bootstrap_token="bt_x_y", callback_token="cb")))
    # Order: immediate DROP -> persist across reboots (G1-6) -> secret exchange -> compose up.
    drop = rc.index("iptables -w -I OUTPUT -d 169.254.169.254 -j DROP")
    persist = rc.index("systemctl enable --now onebrain-metadata-drop.service")
    exchange = rc.index("bash /opt/onebrain/onebrain_bootstrap.sh")
    up = rc.index("up -d")
    assert drop < persist < exchange < up
    # .env-first sourcing so the callbacks' ${VAR} refs re-expand to exchanged secrets.
    assert ". /opt/onebrain/.env 2>/dev/null || true; . /opt/onebrain/box.env" in rc


def test_operator_cloud_init_omits_exchange_but_keeps_drop_persistence():
    # G3-1: the MC box render carries NO exchange step (it bakes .env), but G1-6's
    # metadata-drop persistence still applies to it.
    rc = _runcmd_section(render_cloud_init(_inputs(_ALL, role="operator")))
    assert "onebrain_bootstrap.sh" not in rc
    assert "systemctl enable --now onebrain-metadata-drop.service" in rc


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

"""P4-02: the pure render layer (cloud-init + compose + Caddyfile + env). Golden
files for compose/Caddyfile; assertion-based for env + cloud-init. Regenerate the
goldens with ONEBRAIN_REGEN_GOLDEN=1 (documented here, used only when the intended
output changes)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.provisioning.hetzner.render import (
    BoxRenderInputs,
    render_caddyfile,
    render_cloud_init,
    render_compose,
    render_env_files,
)


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

_GOLDEN = Path(__file__).parent / "golden" / "hetzner"
_ALL = (
    "onebrain-api", "onebrain-admin-ui", "onebrain-workers", "assistant-service",
    "communication-api", "communication-widget", "communication-voice", "communication-workers",
)


def _digest(i: int) -> str:
    return "sha256:" + format(i, "064x")


def _images(modules) -> dict:
    return {m: f"ghcr.io/proark1/{m}@{_digest(idx)}" for idx, m in enumerate(_ALL) if m in set(modules)}


def _inputs(modules, *, fqdn="dep_a.fleet.example", role="customer") -> BoxRenderInputs:
    return BoxRenderInputs(
        deployment_id="dep_a",
        account_id="acct_1",
        compose_project="onebrain-dep_a",
        enabled_modules=tuple(modules),
        images=_images(modules),
        fqdn=fqdn,
        fleet_url="https://mc.example.com",
        fleet_public_desired_state_key="DSPUBKEY",
        release_public_key="RELPUBKEY",
        registry_allowlist="ghcr.io/proark1",
        role=role,
    )


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
    # postgres/redis expose only (never ports:)
    assert "expose:" in compose
    assert "ports:" not in compose


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
    # images map covers exactly the enabled modules (+ infra + migrates)
    module_services = {s for s in services if s not in ("postgres", "redis") and not s.endswith("-migrate")}
    assert module_services == set(_ALL)


# --- env files ---------------------------------------------------------------
_SECRET_KEYS = (
    "POSTGRES_PASSWORD", "REDIS_PASSWORD", "ONEBRAIN_FLEET_KEY",
    "ONEBRAIN_LLM_API_KEY", "ONEBRAIN_SERVICE_KEY", "ONEBRAIN_ADMIN_PASSWORD",
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
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in op
    assert "ONEBRAIN_FLEET_URL=https://mc.example.com" in op        # self-pointing (caller sets the URL)
    assert "ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS=" in op
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=${ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY}" in op
    cust = render_env_files(_inputs(_ALL, role="customer"))["env/onebrain-api.env"]
    assert "ONEBRAIN_IS_OPERATOR_SURFACE" not in cust
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY" not in cust
    assert "ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS" not in cust


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


# --- cloud-init --------------------------------------------------------------
def test_cloud_init_embeds_all_artifacts_and_egress_block():
    ci = render_cloud_init(_inputs(_ALL))
    assert "- python3-cryptography" in ci
    wf = _write_files_section(ci)
    for required in (
        "/opt/onebrain/docker-compose.yml", "/opt/onebrain/Caddyfile", "/opt/onebrain/box.env",
        "/opt/onebrain/postgres-init.sh", "/opt/onebrain/update.sh",
        "/opt/onebrain/onebrain_box_verify.py",
        "/etc/systemd/system/onebrain-update.service", "/etc/systemd/system/onebrain-update.timer",
    ):
        assert f"- path: {required}" in wf
    assert "- path: /opt/onebrain/env/" in wf                        # per-service env files
    assert "set -euo pipefail" in wf                                 # update.sh embedded
    assert "verify_desired_state" in wf                              # verifier embedded
    assert "ExecStart=/opt/onebrain/update.sh" in wf                 # systemd unit embedded
    # both metadata DROP rules (A5, in runcmd) + the {run_id} callback
    rc = _runcmd_section(ci)
    assert "iptables -I DOCKER-USER -d 169.254.169.254 -j DROP" in rc
    assert "iptables -I OUTPUT -d 169.254.169.254 -j DROP" in rc
    assert "/api/provisioning/runs/{run_id}/callback" in ci


def test_cloud_init_compose_calls_are_anchored():
    """First boot: cloud-init runcmd runs with cwd '/', so every `docker compose`
    invocation must carry `-f /opt/onebrain/docker-compose.yml` or Compose V2 finds no
    file and the box never starts (matches update.sh's dc() wrapper)."""
    runcmd = _runcmd_section(render_cloud_init(_inputs(_ALL)))
    compose_lines = [ln for ln in runcmd.splitlines() if "docker compose" in ln]
    assert compose_lines, "expected docker compose calls in runcmd"
    for ln in compose_lines:
        assert "-f /opt/onebrain/docker-compose.yml" in ln, f"unanchored compose call: {ln!r}"


def test_cloud_init_metadata_block_ordering_and_failguard():
    ci = render_cloud_init(_inputs(_ONEBRAIN))
    runcmd = _runcmd_section(ci)   # isolate runcmd; embedded files also contain "up -d"
    guard = runcmd.index("iptables -L DOCKER-USER")
    drop_du = runcmd.index("iptables -I DOCKER-USER -d 169.254.169.254 -j DROP")
    drop_out = runcmd.index("iptables -I OUTPUT -d 169.254.169.254 -j DROP")
    up = runcmd.index("up -d")
    assert guard < drop_du < drop_out < up      # A10 ordering
    assert "metadata_egress_block_failed" in runcmd    # failed insert -> callback failure


def test_update_sh_has_no_crlf():
    # A1: a CRLF checkout would break Cygwin/Git-Bash on `set -euo pipefail\r`.
    from app.provisioning.hetzner import render as R
    raw = (R._DEPLOY_BOX / "update.sh").read_bytes()
    assert b"\r" not in raw


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

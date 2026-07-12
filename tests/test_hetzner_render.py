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


def _inputs(modules, *, fqdn="dep_a.fleet.example", role="customer", run_id="prun_fixture",
            bootstrap_token="", callback_token="") -> BoxRenderInputs:
    return BoxRenderInputs(
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
        registry_allowlist="ghcr.io/proark1",
        role=role,
        bootstrap_token=bootstrap_token,
        callback_token=callback_token,
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
    # The admin seed pair — seed.py (seed_admin_from_env) creates a loginable admin at
    # container start ONLY when BOTH are non-empty. Both are ${VAR} refs filled from the
    # exchanged (customer) / baked (MC) /opt/onebrain/.env; without the email the box seeds
    # no admin and — SSH closed — is unreachable.
    assert "ONEBRAIN_ADMIN_EMAIL=${ONEBRAIN_ADMIN_EMAIL}" in api
    assert "ONEBRAIN_ADMIN_PASSWORD=${ONEBRAIN_ADMIN_PASSWORD}" in api
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
    assert "ONEBRAIN_OPERATOR_MODE" not in cust                     # a customer box is NEVER Mission Control
    assert "ONEBRAIN_IS_OPERATOR_SURFACE" not in cust
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY" not in cust
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS" not in cust
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
    # both metadata DROP rules (A5, in runcmd) + the run-id-substituted callback
    rc = _runcmd_section(ci)
    assert "iptables -I DOCKER-USER -d 169.254.169.254 -j DROP" in rc
    assert "iptables -I OUTPUT -d 169.254.169.254 -j DROP" in rc
    # run_id is baked at render time (no literal placeholder survives), in both the
    # callback URL and box.env — else the box POSTs to /runs/{run_id}/callback and 404s.
    assert "/api/provisioning/runs/prun_fixture/callback" in ci
    assert "ONEBRAIN_RUN_ID=prun_fixture" in ci
    assert "{run_id}" not in ci


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
    wf = _write_files_section(render_cloud_init(_inputs(_ALL, bootstrap_token="bt_x_y", callback_token="cb")))
    assert "- path: /opt/onebrain/onebrain_bootstrap.sh" in wf
    # G1-6: the boot-persistent metadata-egress DROP oneshot is embedded.
    assert "- path: /etc/systemd/system/onebrain-metadata-drop.service" in wf


def test_cloud_init_bootstrap_runcmd_order_and_env_first_source():
    rc = _runcmd_section(render_cloud_init(_inputs(_ALL, bootstrap_token="bt_x_y", callback_token="cb")))
    # Order: immediate DROP -> persist across reboots (G1-6) -> secret exchange -> compose up.
    drop = rc.index("iptables -I OUTPUT -d 169.254.169.254 -j DROP")
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

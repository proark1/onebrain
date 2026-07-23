"""BOOT-CONFIG VALIDATION (go-live regression guard).

The Hetzner render only emits ``${VAR}`` env REFERENCES; the real values arrive on the box
in ``/opt/onebrain/.env`` (the customer box FETCHES it via /bootstrap; the MC box BAKES it
into cloud-init). Golden/text assertions on the render therefore CANNOT catch a box that is
missing a production-boot-essential env var — the gap that shipped onebrain-api with no
ONEBRAIN_AUTH_SECRET (crash on startup) and no ONEBRAIN_ENVIRONMENT (is_production_like
False -> RLS + the safety net silently OFF).

These tests reconstruct the Settings onebrain-api ACTUALLY boots with, for BOTH box kinds,
by resolving the onebrain-api env refs against the box's real ``/opt/onebrain/.env`` (shared
helper, ``tests.boot_config_helper``), then assert the app's OWN boot requirements hold:
the app/main.py cookie-secret guard, validate_runtime_safety, is_production_like + RLS +
postgres, and (MC) operator_mode + the G1-1 active-signer interlock. This FAILS on the
pre-fix tree (auth_secret defaults to the dev value; environment defaults to local) and
passes only once every essential is baked.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from app.config import Settings
from app.controlplane.desired_state import active_signer_in_served_set
from app.deploy.runtime import is_postgres_mode, validate_runtime_safety
from app.fleet.bootstrap_bundle import render_dotenv
from app.fleet.memory import MemoryFleetStore
from app.provisioning.hetzner.broker import InProcessHetznerBroker
from app.provisioning.hetzner.fake import FakeHetznerClient
from app.provisioning.hetzner.provisioner import HetznerProvisioner
from app.provisioning.runs import MemoryProvisioningRunStore
from app.trust.signing import generate_keypair
from tests.boot_config_helper import extract_cloud_init_file, resolve_box_api_settings
from tests.test_bootstrap_mc import _args, _base_argv, _mc_settings, mc
from tests.test_hetzner_provisioner import _control, _open_bundle, _p5_settings, _run

_DEV_DEFAULT = "dev-insecure-change-me"


def _auth_guard_passes(s: Settings) -> bool:
    """The EXACT fail-closed condition at app/main.py create_app (line 34), inverted: the
    box boots only when the cookie secret is neither the dev default nor shorter than 32."""
    return not (s.auth_secret == _DEV_DEFAULT or len(s.auth_secret) < 32)


def _assert_common_boot_requirements(s: Settings) -> None:
    # 1. app/main.py's cookie-secret guard passes (a weak/default secret RuntimeErrors the api).
    assert _auth_guard_passes(s), f"auth_secret guard would fail closed: {s.auth_secret!r}"
    # 2. production-like -> validate_runtime_safety's net is ARMED, and it is SATISFIED
    #    (pgvector + a real DSN + RLS). Both the arming and the satisfaction must hold.
    assert s.is_production_like is True, "environment is not production-like; the safety net is skipped"
    assert s.rls_enforced is True, "RLS not enforced; tenant isolation would be OFF"
    assert is_postgres_mode(s) is True
    validate_runtime_safety(s)   # must NOT raise
    # 3. every box is behind Caddy TLS -> secure session cookies.
    assert s.cookie_secure is True


# --- MC (Mission Control) box ------------------------------------------------

def _mc_boot_settings():
    # A signing-enabled MC (private key + its derived public key in the served set) so the
    # active-signer interlock is a MEANINGFUL True, not the inert no-key default.
    priv, pub = generate_keypair()
    settings = _mc_settings(fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub)
    art = mc.build_mc_artifacts(_args(_base_argv()), settings)
    api_env = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/env/onebrain-api.env")
    dotenv = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/.env")   # MC bakes its own .env
    return resolve_box_api_settings(api_env, dotenv), art, pub


def test_mc_box_env_satisfies_onebrain_api_boot_requirements():
    settings, art, pub = _mc_boot_settings()
    _assert_common_boot_requirements(settings)
    # The resolved cookie secret is exactly the strong per-box value baked into /opt/onebrain/.env
    # (proves the ${VAR} ref really interpolated from the baked dotenv, not a default).
    assert settings.auth_secret == art.bundle["ONEBRAIN_AUTH_SECRET"]
    assert len(settings.auth_secret) == 64
    assert settings.login_rate_limit_secret == art.bundle["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"]
    assert len(settings.login_rate_limit_secret) == 64
    # MC-specific: Mission Control is armed and passes the G1-1 startup interlock app/main.py
    # asserts under operator_mode (active signer present in the served wrapper-key set).
    assert settings.operator_mode is True
    assert active_signer_in_served_set(settings) is True
    assert settings.fleet_desired_state_public_keys == pub


def test_mc_box_threads_operator_control_plane_settings():
    """Every operator control-plane knob must reach the onebrain-api container on a
    PROVISIONED MC (no SSH). A value baked only into /opt/onebrain/.env is used purely to
    interpolate ${VAR} refs and never becomes a Setting (resolve_box_api_settings), so before
    this fix an operator could bake e.g. operator_self_max_attempts=1 and the box still
    resolved 3. Guards the render._OPERATOR_APP_CONTROL_ENV <-> bootstrap_mc overlay pair from
    drifting back to that silent-default state."""
    from app.provisioning.hetzner.render import _OPERATOR_APP_CONTROL_ENV

    priv, pub = generate_keypair()
    settings = _mc_settings(
        fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub,
        operator_auto_deploy_enabled=True,
        dev_release_verify_public_key="dev-verify-key-xyz",
        operator_self_max_attempts=1,
        operator_self_retry_backoff_seconds=111,
        development_auto_retry_enabled=True,
        development_auto_retry_max_attempts=9,
        development_auto_retry_backoff_seconds=222,
        development_auto_retry_backup_backoff_seconds=333,
        pipeline_stall_alert_seconds=0,   # a deliberate DISABLE, not a default — must survive as 0
        fleet_alert_webhook_url="https://hooks.example.com/x?a=1",
    )
    art = mc.build_mc_artifacts(_args(_base_argv()), settings)
    api_env = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/env/onebrain-api.env")
    dotenv = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/.env")
    s = resolve_box_api_settings(api_env, dotenv)

    # These two were latently overlay-only before this fix: operator_auto_deploy gates the
    # whole self-deploy feature, and the app needs the dev verify key to register dev candidates.
    assert s.operator_auto_deploy_enabled is True
    assert s.dev_release_verify_public_key == "dev-verify-key-xyz"
    # MC self-deploy bounded retry (#69): max_attempts=1 must restore single-attempt behavior.
    assert s.operator_self_max_attempts == 1
    assert s.operator_self_retry_backoff_seconds == 111
    # Dev-pipeline auto-retry (#61).
    assert s.development_auto_retry_enabled is True
    assert s.development_auto_retry_max_attempts == 9
    assert s.development_auto_retry_backoff_seconds == 222
    assert s.development_auto_retry_backup_backoff_seconds == 333
    # Pipeline-stall detection + delivery (#65). 0 is a deliberate disable and must survive as 0.
    assert s.pipeline_stall_alert_seconds == 0
    assert s.fleet_alert_webhook_url == "https://hooks.example.com/x?a=1"

    # Drift guard: every declared operator knob maps to a real Settings field (a typo or a
    # non-field name would be silently dropped by resolve_box_api_settings/pydantic).
    fields = set(Settings.model_fields)
    for name in _OPERATOR_APP_CONTROL_ENV:
        assert name.startswith("ONEBRAIN_"), name
        assert name[len("ONEBRAIN_"):].lower() in fields, name


def test_mc_box_threads_candidate_registration_credential(monkeypatch):
    """The CI dev-candidate credential (POST /api/operator/release-candidates auth) must reach a
    PROVISIONED MC (no SSH). The hash is stored "sha256$<hex>", and docker-compose would rewrite
    the literal '$' when it interpolates the box's baked /opt/onebrain/.env on first boot — so
    bootstrap_mc percent-encodes it as %24 and operator._require_candidate_auth reverses it before
    verify_secret. Prove the exact id + hash resolve on the box and that _require_candidate_auth
    accepts a CI token bearing them (the whole dev-candidate pipeline is 401 on a rendered MC
    without this)."""
    import pytest
    from fastapi import HTTPException

    from app.fleet.keys import hash_secret
    from app.routers import operator as operator_router

    secret = "ci-candidate-secret-token"
    priv, pub = generate_keypair()
    settings = _mc_settings(
        fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub,
        release_candidate_key_id="candidate-ci-v1",
        release_candidate_key_hash=hash_secret(secret))
    art = mc.build_mc_artifacts(_args(_base_argv()), settings)
    api_env = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/env/onebrain-api.env")
    dotenv = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/.env")
    s = resolve_box_api_settings(api_env, dotenv)

    # The id threads verbatim; the hash rides '$'-free on the box (compose cannot rewrite it),
    # and _candidate_hash_from_env recovers the EXACT configured "sha256$<hex>".
    assert s.release_candidate_key_id == "candidate-ci-v1"
    assert s.release_candidate_key_hash and "$" not in s.release_candidate_key_hash
    assert operator_router._candidate_hash_from_env(s.release_candidate_key_hash) == hash_secret(secret)

    # End-to-end: the operator router, reading the box's resolved Settings, accepts a CI token
    # signed with the configured secret and rejects a wrong one (401).
    monkeypatch.setattr(operator_router, "get_settings", lambda: s)
    assert operator_router._require_candidate_auth(
        f"Bearer {secret}", "candidate-ci-v1") == "candidate:candidate-ci-v1"
    with pytest.raises(HTTPException) as exc:
        operator_router._require_candidate_auth("Bearer wrong-token", "candidate-ci-v1")
    assert exc.value.status_code == 401


# --- customer box ------------------------------------------------------------

def _customer_boot_settings(*, bundle_overrides: dict | None = None):
    control = _control()
    prov = MemoryProvisioningRunStore()
    fleet = MemoryFleetStore()
    fake = FakeHetznerClient()
    settings = _p5_settings()
    HetznerProvisioner(settings, InProcessHetznerBroker(fake), control,
                       prov_store=prov, fleet_store=fleet).dispatch(
        _run(prov), owner_otp="owner-otp", service_key="sk_abc", space_id="space_1",
        owner_email="owner@example.com")
    # onebrain-api.env is rendered into cloud-init; the box FETCHES /opt/onebrain/.env via
    # /bootstrap, whose body is render_dotenv(stored bundle).
    api_env = extract_cloud_init_file(fake.servers[0].user_data, "/opt/onebrain/env/onebrain-api.env")
    bundle = _open_bundle(prov, settings, "dep_a")
    if bundle_overrides:
        bundle.update(bundle_overrides)
    return resolve_box_api_settings(api_env, render_dotenv(bundle)), bundle


def test_customer_box_env_satisfies_onebrain_api_boot_requirements():
    settings, bundle = _customer_boot_settings()
    _assert_common_boot_requirements(settings)
    # The cookie secret is the strong per-box value delivered inside the bundle dotenv.
    assert settings.auth_secret == bundle["ONEBRAIN_AUTH_SECRET"]
    assert len(settings.auth_secret) == 64
    assert settings.login_rate_limit_secret == bundle["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"]
    assert len(settings.login_rate_limit_secret) == 64
    # A customer box is NEVER Mission Control (no operator overlay).
    assert settings.operator_mode is False


def test_customer_connection_url_encodes_legacy_reserved_password_characters():
    raw = "legacy@password:/?[]+=%"
    settings, _ = _customer_boot_settings(bundle_overrides={"POSTGRES_APP_PASSWORD": raw})

    parsed = urlsplit(settings.database_url)
    assert parsed.username == "onebrain_app"
    assert parsed.hostname == "postgres"
    assert parsed.path == "/onebrain"
    assert unquote(parsed.password or "") == raw

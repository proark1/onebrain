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
    # MC-specific: Mission Control is armed and passes the G1-1 startup interlock app/main.py
    # asserts under operator_mode (active signer present in the served wrapper-key set).
    assert settings.operator_mode is True
    assert active_signer_in_served_set(settings) is True
    assert settings.fleet_desired_state_public_keys == pub


# --- customer box ------------------------------------------------------------

def _customer_boot_settings():
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
    return resolve_box_api_settings(api_env, render_dotenv(bundle)), bundle


def test_customer_box_env_satisfies_onebrain_api_boot_requirements():
    settings, bundle = _customer_boot_settings()
    _assert_common_boot_requirements(settings)
    # The cookie secret is the strong per-box value delivered inside the bundle dotenv.
    assert settings.auth_secret == bundle["ONEBRAIN_AUTH_SECRET"]
    assert len(settings.auth_secret) == 64
    # A customer box is NEVER Mission Control (no operator overlay).
    assert settings.operator_mode is False

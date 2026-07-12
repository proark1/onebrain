"""P5-06: the MC bootstrap runner (scripts/bootstrap_mc.py). Dry-run renders a valid
operator-overlay cloud-init with the bundle BAKED as /opt/onebrain/.env (G3-1) and NO
bootstrap token / exchange step; the create path drives an INJECTED FakeHetznerClient
(default-deny firewall + server carrying the operator user-data) and never echoes a
secret; a tokenless --no-dry-run aborts without constructing a client. No live Hetzner
call, ever.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.provisioning.hetzner.fake import FakeHetznerClient
from app.trust.signing import generate_keypair

# Load the non-package script by path (mirrors tests/test_box_verify.py).
_MC_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_mc.py"
_spec = importlib.util.spec_from_file_location("bootstrap_mc", _MC_PATH)
mc = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["bootstrap_mc"] = mc
_spec.loader.exec_module(mc)

_D = "sha256:" + "a" * 64
_IMAGES = json.dumps({m: f"ghcr.io/proark1/{m}@{_D}" for m in mc._MC_MODULES})


def _args(argv):
    return mc._build_parser().parse_args(argv)


def _base_argv(*extra):
    return ["--fleet-public-url", "https://mc.example.com", "--deployment-id", "mc",
            "--images-json", _IMAGES, *extra]


def _mc_settings(**over):
    """MC operator Settings with the admin login present by default. ONEBRAIN_ADMIN_EMAIL is
    a required bundle key now (the MC box seeds its own admin at first boot), so
    build_mc_artifacts fails closed without it — every test that expects a valid render must
    supply it."""
    data = dict(admin_email="mc-admin@example.com")
    data.update(over)
    return Settings(**data)


# --- dry-run: the baked operator cloud-init (G3-1) ---------------------------

def test_dry_run_bakes_operator_env_and_omits_bootstrap_token():
    settings = _mc_settings()
    art = mc.build_mc_artifacts(_args(_base_argv("--fqdn", "mc.example.com")), settings)
    ci = art.cloud_init

    # Operator overlay (A14) + the desired-state PRIVATE key as a ${VAR} ref.
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in ci
    # The MC box is actually armed as Mission Control: operator_mode is baked BOTH as the
    # onebrain-api env literal (the settable field is_operator_surface does NOT set) and in
    # the baked /opt/onebrain/.env overlay — without it the whole fleet surface is dormant.
    assert "ONEBRAIN_OPERATOR_MODE=true" in ci
    assert ci.count("ONEBRAIN_OPERATOR_MODE=true") >= 2      # onebrain-api.env literal + baked .env overlay
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=${ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY}" in ci
    # The baked /opt/onebrain/.env carries the foundational secrets with REAL values.
    assert "/opt/onebrain/.env" in ci
    assert f"POSTGRES_PASSWORD={art.bundle['POSTGRES_PASSWORD']}" in ci
    assert f"ONEBRAIN_FLEET_KEY={art.fleet_token}" in ci
    # G3-1: the MC box is BAKED, never exchanged — no first-boot token is baked, and no
    # /bootstrap exchange step runs in the runcmd.
    assert "ONEBRAIN_BOOTSTRAP_TOKEN=" not in ci
    assert "bash /opt/onebrain/onebrain_bootstrap.sh" not in ci


def test_dry_run_main_exit_zero_no_client_and_redacts_secrets(capsys):
    # A configured desired-state private key (with its derived pub in the served set so the
    # G1-1 preflight passes) proves the private key is REDACTED in the printed cloud-init.
    priv, pub = generate_keypair()
    settings = _mc_settings(fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub)
    fake = FakeHetznerClient()

    rc = mc.main(_base_argv("--dry-run"), settings=settings, client=fake)

    assert rc == 0
    assert fake.calls == []  # dry-run makes NO client call
    out = capsys.readouterr().out
    assert "***REDACTED***" in out
    assert priv not in out                                   # the crown-jewel key never printed
    # A generated bundle secret value never appears verbatim either (redacted in the .env dump).
    assert not re.search(r"POSTGRES_PASSWORD=[A-Za-z0-9_-]{20,}", out)


def _last_env_value(ci: str, key: str):
    """The LAST `KEY=value` in the rendered cloud-init. The baked /opt/onebrain/.env is
    written AFTER env/onebrain-api.env, so the last occurrence is the real baked value
    (the earlier one is the onebrain-api.env ${VAR} ref)."""
    matches = re.findall(rf"(?m)^\s*{re.escape(key)}=(.*)$", ci)
    return matches[-1] if matches else None


def test_signing_mc_bakes_public_key_set_so_g1_1_startup_passes():
    # A signing-enabled MC (private key set) must bake its APP-level accepted wrapper-key
    # SET, or it fails its OWN G1-1 startup assertion the moment operator_mode is on (finding
    # #1) — an unbootable box the instant MC becomes a real control plane.
    from app.controlplane.desired_state import active_signer_in_served_set, active_wrapper_public_key

    priv, pub = generate_keypair()
    settings = _mc_settings(fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub)
    ci = mc.build_mc_artifacts(_args(_base_argv()), settings).cloud_init

    # onebrain-api.env references it as a ${VAR}; the baked .env supplies the real value.
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS=${ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS}" in ci
    assert _last_env_value(ci, "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS") == pub
    assert _last_env_value(ci, "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY") == priv
    assert _last_env_value(ci, "ONEBRAIN_OPERATOR_MODE") == "true"

    # Reconstruct the MC box's OWN Settings from the baked values: a signing-enabled MC does
    # NOT brick — its active signer is in its served set (active_signer_in_served_set True),
    # which is exactly what app/main.py asserts at startup under operator_mode.
    box = Settings(
        operator_mode=True,
        fleet_desired_state_private_key=_last_env_value(ci, "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY"),
        fleet_desired_state_public_keys=_last_env_value(ci, "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS"),
    )
    assert box.operator_mode is True
    assert active_wrapper_public_key(box) == pub
    assert active_signer_in_served_set(box) is True


# --- create path: injected client, default-deny firewall, secret hygiene -----

def test_create_path_drives_injected_client_and_hides_secrets(capsys):
    settings = _mc_settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                            hetzner_api_token="tok", hetzner_volume_size_gb=0)
    fake = FakeHetznerClient()

    rc = mc.main(_base_argv("--fqdn", "mc.example.com", "--no-dry-run"), settings=settings, client=fake)

    assert rc == 0
    assert fake.calls.count("create_firewall") == 1 and fake.calls.count("create_server") == 1
    # Default-deny firewall: inbound tcp 80 + 443, and NO port-22 rule.
    ports = {r.port for r in fake.firewalls[0].rules}
    assert ports == {"80", "443"}
    # The server carries the OPERATOR cloud-init as user_data, with the firewall attached in-create.
    server = fake.servers[0]
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in server.user_data
    assert server.firewall_ids and server.firewall_ids[-1] == "fw_1"
    # Secret hygiene: the create path prints ONLY the shape — the cloud-init (with every
    # baked secret) is NEVER dumped, and a real baked secret never appears in the capture.
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert server.user_data not in text
    baked_pw = re.search(r"POSTGRES_PASSWORD=([A-Za-z0-9_-]{20,})", server.user_data).group(1)
    assert baked_pw not in text


def test_create_with_allow_ssh_adds_port_22_rule():
    settings = _mc_settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                            hetzner_api_token="tok", hetzner_firewall_allow_ssh=True)
    fake = FakeHetznerClient()

    assert mc.main(_base_argv("--no-dry-run"), settings=settings, client=fake) == 0
    ports = {r.port for r in fake.firewalls[0].rules}
    assert ports == {"80", "443", "22"}   # break-glass SSH rule present


# --- DNS: zone-relative label, not the full fqdn -----------------------------

def test_dns_record_uses_zone_relative_label_not_fqdn():
    # Hetzner DNS treats a name without a trailing dot as RELATIVE to the zone, so the A
    # record must carry the LABEL "mc" (deployment_id), not "mc.onlyonebrain.com" — the
    # latter resolves as "mc.onlyonebrain.com.onlyonebrain.com" and the MC box's own
    # self-heartbeat to --fleet-public-url never resolves.
    # DNS auth is now the unified Cloud token (hetzner_api_token) — no separate DNS token
    # is needed to build the record shape; provider + zone + fqdn are the only gates.
    settings = _mc_settings(fleet_dns_provider="hetzner", fleet_dns_zone_id="zone_ob")
    art = mc.build_mc_artifacts(_args(_base_argv("--fqdn", "mc.onlyonebrain.com")), settings)
    assert art.dns is not None
    assert art.dns.name == "mc"                       # zone-relative label, NOT the fqdn
    assert art.dns.zone_id == "zone_ob"
    # ...but the box hostname / Caddy TLS site is still the full fqdn.
    assert "mc.onlyonebrain.com {" in art.cloud_init
    assert art.dns.name != "mc.onlyonebrain.com"


# --- fail-closed gates -------------------------------------------------------

def test_tokenless_no_dry_run_aborts_without_client():
    settings = _mc_settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                            hetzner_api_token="")  # NO token
    fake = FakeHetznerClient()

    rc = mc.main(_base_argv("--no-dry-run"), settings=settings, client=fake)

    assert rc == 2
    assert fake.calls == []  # never constructed/called the client


def test_images_json_must_cover_mc_modules():
    settings = Settings()
    partial = json.dumps({"onebrain-api": f"ghcr.io/proark1/onebrain-api@{_D}"})  # missing two modules
    rc = mc.main(["--fleet-public-url", "https://mc.example.com", "--images-json", partial, "--dry-run"],
                 settings=settings)
    assert rc == 2


def test_g1_1_preflight_refuses_excluding_pubkey_set():
    # A configured active signer whose derived pub is NOT in the served set would brick the
    # MC box on its own G1-1 startup assertion — refuse to bake it (fail closed).
    priv, _pub = generate_keypair()
    settings = Settings(fleet_desired_state_private_key=priv, fleet_desired_state_public_keys="")
    with pytest.raises(ValueError, match="active_signer_not_in_public_key_set"):
        mc.build_mc_artifacts(_args(_base_argv()), settings)
    # ...and main() surfaces it as a clean non-zero exit, no client.
    fake = FakeHetznerClient()
    assert mc.main(_base_argv("--dry-run"), settings=settings, client=fake) == 2
    assert fake.calls == []


# --- admin login: the MC box must seed a loginable admin (go-live blocker) ----

def test_build_mc_artifacts_fails_closed_without_admin_email():
    # The MC box seeds its own admin from ONEBRAIN_ADMIN_EMAIL/PASSWORD at first boot; SSH is
    # closed, so a box with no admin email is a box the operator can never log into. FAIL CLOSED.
    settings = Settings()   # no ONEBRAIN_ADMIN_EMAIL; no private key -> G1-1 preflight is inert
    with pytest.raises(ValueError, match="ONEBRAIN_ADMIN_EMAIL required"):
        mc.build_mc_artifacts(_args(_base_argv()), settings)
    # main() surfaces it as a clean non-zero exit, no client constructed/called.
    fake = FakeHetznerClient()
    assert mc.main(_base_argv("--dry-run"), settings=settings, client=fake) == 2
    assert fake.calls == []


def test_build_mc_artifacts_bundle_carries_admin_login():
    # With an admin email + an operator-set ONEBRAIN_ADMIN_PASSWORD, the bundle carries BOTH
    # (seed.py makes a loginable admin), the email is normalized, and the password is NOT
    # flagged generated. The baked /opt/onebrain/.env carries both REAL values.
    settings = _mc_settings(admin_email="Ops@OnlyOneBrain.com", admin_password="env-set-pw-xyz")
    art = mc.build_mc_artifacts(_args(_base_argv()), settings)
    assert art.bundle["ONEBRAIN_ADMIN_EMAIL"] == "ops@onlyonebrain.com"   # normalized to match seed-time lookup
    assert art.bundle["ONEBRAIN_ADMIN_PASSWORD"] == "env-set-pw-xyz"
    assert art.admin_email == "ops@onlyonebrain.com"
    assert art.admin_password == "env-set-pw-xyz"
    assert art.admin_password_generated is False
    # onebrain-api.env references both as ${VAR}; the baked .env supplies the real values.
    assert "ONEBRAIN_ADMIN_EMAIL=${ONEBRAIN_ADMIN_EMAIL}" in art.cloud_init
    assert _last_env_value(art.cloud_init, "ONEBRAIN_ADMIN_EMAIL") == "ops@onlyonebrain.com"
    assert _last_env_value(art.cloud_init, "ONEBRAIN_ADMIN_PASSWORD") == "env-set-pw-xyz"


def test_build_mc_artifacts_generates_admin_password_when_unset():
    # No operator ONEBRAIN_ADMIN_PASSWORD -> a fresh password is minted, flagged generated, and
    # is exactly what lands in the bundle (so the seeded admin's password == the surfaced one).
    art = mc.build_mc_artifacts(_args(_base_argv()), _mc_settings(admin_password=""))
    assert art.admin_password_generated is True
    assert art.admin_password and art.admin_password == art.bundle["ONEBRAIN_ADMIN_PASSWORD"]


def test_create_surfaces_admin_login_out_of_band_and_never_dumps_cloud_init(capsys):
    # The create path surfaces the login (email + password) OUT-OF-BAND to the operator's own
    # terminal — the ONE credential they must keep — and the surfaced password is exactly the
    # one baked into the box's /opt/onebrain/.env. The cloud-init/user-data is NEVER dumped.
    settings = _mc_settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                            hetzner_api_token="tok", hetzner_volume_size_gb=0,
                            admin_email="ops@onlyonebrain.com", admin_password="")  # generated
    fake = FakeHetznerClient()
    assert mc.main(_base_argv("--no-dry-run"), settings=settings, client=fake) == 0
    out = capsys.readouterr().out

    server = fake.servers[0]
    baked_pw = re.search(r"ONEBRAIN_ADMIN_PASSWORD=([A-Za-z0-9_-]{20,})", server.user_data).group(1)
    # The generated password is surfaced out-of-band alongside the email...
    assert f"SAVE THIS - Mission Control admin login: ops@onlyonebrain.com / {baked_pw}" in out
    # ...but the full cloud-init (carrying every baked secret) is NEVER printed.
    assert server.user_data not in out


def test_dry_run_surfaces_admin_email_but_never_the_password(capsys):
    # Dry run surfaces the admin EMAIL (not a secret) and notes where the password comes from,
    # but prints NO real password value: a generated one is redacted in the cloud-init dump and
    # withheld from the note (it is shown only on --no-dry-run).
    settings = _mc_settings(admin_email="ops@onlyonebrain.com", admin_password="")  # generated
    fake = FakeHetznerClient()
    assert mc.main(_base_argv("--dry-run"), settings=settings, client=fake) == 0
    out = capsys.readouterr().out
    assert "ops@onlyonebrain.com" in out
    assert "will be GENERATED and printed once on --no-dry-run" in out
    # No real baked password value ever appears (redacted in the cloud-init dump; not in the note).
    assert not re.search(r"ONEBRAIN_ADMIN_PASSWORD=[A-Za-z0-9_-]{20,}", out)
    assert "***REDACTED***" in out

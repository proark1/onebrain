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


# --- dry-run: the baked operator cloud-init (G3-1) ---------------------------

def test_dry_run_bakes_operator_env_and_omits_bootstrap_token():
    settings = Settings()
    art = mc.build_mc_artifacts(_args(_base_argv("--fqdn", "mc.example.com")), settings)
    ci = art.cloud_init

    # Operator overlay (A14) + the desired-state PRIVATE key as a ${VAR} ref.
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in ci
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
    settings = Settings(fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub)
    fake = FakeHetznerClient()

    rc = mc.main(_base_argv("--dry-run"), settings=settings, client=fake)

    assert rc == 0
    assert fake.calls == []  # dry-run makes NO client call
    out = capsys.readouterr().out
    assert "***REDACTED***" in out
    assert priv not in out                                   # the crown-jewel key never printed
    # A generated bundle secret value never appears verbatim either (redacted in the .env dump).
    assert not re.search(r"POSTGRES_PASSWORD=[A-Za-z0-9_-]{20,}", out)


# --- create path: injected client, default-deny firewall, secret hygiene -----

def test_create_path_drives_injected_client_and_hides_secrets(capsys):
    settings = Settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
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
    settings = Settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                        hetzner_api_token="tok", hetzner_firewall_allow_ssh=True)
    fake = FakeHetznerClient()

    assert mc.main(_base_argv("--no-dry-run"), settings=settings, client=fake) == 0
    ports = {r.port for r in fake.firewalls[0].rules}
    assert ports == {"80", "443", "22"}   # break-glass SSH rule present


# --- fail-closed gates -------------------------------------------------------

def test_tokenless_no_dry_run_aborts_without_client():
    settings = Settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
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

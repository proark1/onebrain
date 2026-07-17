"""P5-06: the MC bootstrap runner (scripts/bootstrap_mc.py). Dry-run renders a valid
operator-overlay cloud-init with the bundle BAKED as /opt/onebrain/.env (G3-1) and NO
bootstrap token / exchange step; the create path drives an INJECTED FakeHetznerClient
(default-deny firewall + server carrying the operator user-data) and never echoes a
secret; a tokenless --no-dry-run aborts without constructing a client. No live Hetzner
call, ever.
"""

from __future__ import annotations

import importlib.util
import base64
import datetime as dt
import gzip
import io
import json
import re
import tarfile
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.config import Settings
from app.provisioning.hetzner.fake import FakeHetznerClient
from app.trust.signing import generate_keypair
from tests.boot_config_helper import extract_cloud_init_file, resolve_box_api_settings

# Load the non-package script by path (mirrors tests/test_box_verify.py).
_MC_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_mc.py"
_spec = importlib.util.spec_from_file_location("bootstrap_mc", _MC_PATH)
mc = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["bootstrap_mc"] = mc
_spec.loader.exec_module(mc)

_D = "sha256:" + "a" * 64
_IMAGES = json.dumps({m: f"ghcr.io/proark1/{m}@{_D}" for m in mc._MC_MODULES})


def _asset_text(cloud_init: str, path: str) -> str:
    match = re.search(
        r"  - path: /opt/onebrain/onebrain-assets\.tar\n"
        r"    permissions: '[0-7]+'\n"
        r"    encoding: gz\+b64\n"
        r"    content: (?P<blob>\S+)\n",
        cloud_init,
    )
    assert match
    archive = gzip.decompress(base64.b64decode(match.group("blob")))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        handle = tar.extractfile(path.lstrip("/"))
        assert handle is not None
        return handle.read().decode("utf-8")


def _tls_asset(cloud_init: str, path: str) -> tuple[str, int]:
    """Read one MC-only broker TLS asset from its redaction-friendly archive."""
    match = re.search(
        r"  - path: /opt/onebrain/mc-broker-tls\.tar\n"
        r"    permissions: '[0-7]+'\n"
        r"    encoding: gz\+b64\n"
        r"    content: (?P<blob>\S+)\n",
        cloud_init,
    )
    assert match
    archive = gzip.decompress(base64.b64decode(match.group("blob")))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        member = tar.getmember(path.lstrip("/"))
        handle = tar.extractfile(member)
        assert handle is not None
        return handle.read().decode("utf-8"), member.mode


def _write_mtls_files(tmp_path):
    """Small, real P-256 mTLS material (valid for ssl.load_cert_chain)."""
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OneBrain test broker CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "onebrain-mc")])
    client = (
        x509.CertificateBuilder()
        .subject_name(client_name)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    cert = tmp_path / "mc-client.crt"
    key = tmp_path / "mc-client.key"
    ca_path = tmp_path / "broker-ca.crt"
    cert.write_bytes(client.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return cert, key, ca_path


def _production_mc_settings(tmp_path, **overrides):
    cert, key, ca = _write_mtls_files(tmp_path)
    desired_private, desired_public = generate_keypair()
    _release_private, release_public = generate_keypair()
    values = {
        "environment": "production",
        "admin_email": "mc-admin@example.com",
        # The one-time bootstrap workstation has this token. The rendered MC
        # must prove it did NOT receive it and uses the remote broker instead.
        "hetzner_api_token": "initial-hcloud-token",
        "hetzner_firewall_id": "fw-existing",
        "fleet_dns_provider": "hetzner",
        "fleet_dns_zone_id": "zone-1",
        "fleet_base_domain": "example.com",
        "provisioner_backend": "hetzner",
        "hetzner_allow_inprocess_broker": False,
        "hetzner_broker_url": "https://broker.example.com",
        "hetzner_broker_credential": "broker-credential-secret",
        "hetzner_broker_client_certificate_file": str(cert),
        "hetzner_broker_client_key_file": str(key),
        "hetzner_broker_ca_file": str(ca),
        "secret_encryption_key": Fernet.generate_key().decode("ascii"),
        "provisioning_callback_allowed_hosts": "mc.example.com",
        "fleet_desired_state_private_key": desired_private,
        "fleet_desired_state_public_keys": desired_public,
        "fleet_desired_state_ttl_seconds": 900,
        "fleet_reconcile_seconds": 60,
        "release_verify_public_key": release_public,
        "release_registry_allowlist": "ghcr.io/proark1",
        "release_require_signature": True,
        "release_require_signed_images": True,
        "release_require_rollback_kind": True,
        "release_promotion_required": True,
        "postgres_app_role": "onebrain_app",
        "postgres_worker_role": "onebrain_worker",
        "postgres_assistant_role": "assistant_app",
        "postgres_communication_role": "communication_app",
    }
    values.update(overrides)
    return Settings(**values)


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
    api_env = _asset_text(ci, "/opt/onebrain/env/onebrain-api.env")

    # Operator overlay (A14) + the desired-state PRIVATE key as a ${VAR} ref.
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in api_env
    # The MC box is actually armed as Mission Control: operator_mode is baked BOTH as the
    # onebrain-api env literal (the settable field is_operator_surface does NOT set) and in
    # the baked /opt/onebrain/.env overlay — without it the whole fleet surface is dormant.
    assert "ONEBRAIN_OPERATOR_MODE=true" in api_env
    dotenv = extract_cloud_init_file(ci, "/opt/onebrain/.env")
    assert "ONEBRAIN_OPERATOR_MODE=true" in dotenv            # baked .env overlay
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=${ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY}" in api_env
    # The baked /opt/onebrain/.env carries the foundational secrets with REAL values.
    assert "POSTGRES_PASSWORD=" in dotenv
    assert f"POSTGRES_PASSWORD={art.bundle['POSTGRES_PASSWORD']}" in dotenv
    assert f"ONEBRAIN_FLEET_KEY={art.fleet_token}" in dotenv
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
    """The `KEY=value` in the MC's baked /opt/onebrain/.env asset."""
    matches = re.findall(
        rf"(?m)^\s*{re.escape(key)}=(.*)$",
        extract_cloud_init_file(ci, "/opt/onebrain/.env"),
    )
    return matches[-1] if matches else None


def test_signing_mc_bakes_public_key_set_so_g1_1_startup_passes():
    # A signing-enabled MC (private key set) must bake its APP-level accepted wrapper-key
    # SET, or it fails its OWN G1-1 startup assertion the moment operator_mode is on (finding
    # #1) — an unbootable box the instant MC becomes a real control plane.
    from app.controlplane.desired_state import active_signer_in_served_set, active_wrapper_public_key

    priv, pub = generate_keypair()
    settings = _mc_settings(fleet_desired_state_private_key=priv, fleet_desired_state_public_keys=pub)
    ci = mc.build_mc_artifacts(_args(_base_argv()), settings).cloud_init
    api_env = _asset_text(ci, "/opt/onebrain/env/onebrain-api.env")

    # onebrain-api.env references it as a ${VAR}; the baked .env supplies the real value.
    assert "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS=${ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS}" in api_env
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


def test_production_mc_render_passes_its_own_preflight_and_isolates_mtls(tmp_path):
    """The go-live artifact, not merely the source workstation, is bootable.

    The workstation has a short-lived direct HCloud token so it can create the
    first box. The rendered API must instead contain only remote-broker config,
    scoped mTLS material, and the complete production-preflight configuration.
    """
    source = _production_mc_settings(tmp_path)
    art = mc.build_mc_artifacts(
        _args(_base_argv("--fqdn", "mc.example.com")), source)
    assert len(art.cloud_init.encode("utf-8")) < 32_768
    assert "initial-hcloud-token" not in art.cloud_init
    assert art.bundle["ONEBRAIN_DNS_TOKEN"] == ""

    api_env = _asset_text(art.cloud_init, "/opt/onebrain/env/onebrain-api.env")
    dotenv = extract_cloud_init_file(art.cloud_init, "/opt/onebrain/.env")
    assert "ONEBRAIN_PROVISIONER_BACKEND=hetzner" in api_env
    assert "ONEBRAIN_HETZNER_ALLOW_INPROCESS_BROKER=false" in api_env
    assert "ONEBRAIN_HETZNER_BROKER_URL=https://broker.example.com" in api_env
    assert "ONEBRAIN_HETZNER_BROKER_CREDENTIAL=${ONEBRAIN_HETZNER_BROKER_CREDENTIAL}" in api_env
    assert "ONEBRAIN_SECRET_ENCRYPTION_KEY=${ONEBRAIN_SECRET_ENCRYPTION_KEY}" in api_env
    assert "ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true" in api_env
    assert "ONEBRAIN_FLEET_RECONCILE_SECONDS=60" in api_env
    assert f"ONEBRAIN_HETZNER_BROKER_CREDENTIAL={source.hetzner_broker_credential}" in dotenv
    assert f"ONEBRAIN_SECRET_ENCRYPTION_KEY={source.secret_encryption_key}" in dotenv

    # The MC key/cert/CA are packaged with 0400 modes and the rendered API is
    # the only service that bind-mounts the directory read-only.
    emitted = tmp_path / "rendered-tls"
    emitted.mkdir()
    tls_paths = {
        "mc-client.crt": "/opt/onebrain/broker-tls/mc-client.crt",
        "mc-client.key": "/opt/onebrain/broker-tls/mc-client.key",
        "broker-ca.crt": "/opt/onebrain/broker-tls/broker-ca.crt",
    }
    local_paths = {}
    for name, cloud_path in tls_paths.items():
        content, mode = _tls_asset(art.cloud_init, cloud_path)
        assert mode == 0o400
        local = emitted / name
        local.write_text(content, encoding="utf-8")
        local.chmod(0o400)
        local_paths[name] = str(local)

    compose = _asset_text(art.cloud_init, "/opt/onebrain/docker-compose.yml")
    assert compose.count("/opt/onebrain/broker-tls:/run/onebrain/broker-tls:ro") == 1
    resolved = resolve_box_api_settings(api_env, dotenv).model_copy(update={
        "hetzner_broker_client_certificate_file": local_paths["mc-client.crt"],
        "hetzner_broker_client_key_file": local_paths["mc-client.key"],
        "hetzner_broker_ca_file": local_paths["broker-ca.crt"],
    })
    assert resolved.hetzner_api_token == ""
    resolved.assert_production_mission_control_ready()


def test_production_mc_bootstrap_uses_initial_client_but_never_bakes_its_token(tmp_path, capsys):
    source = _production_mc_settings(tmp_path)
    fake = FakeHetznerClient()

    rc = mc.main(
        _base_argv("--fqdn", "mc.example.com", "--no-dry-run"),
        settings=source,
        client=fake,
    )

    assert rc == 0
    assert "create_server" in fake.calls
    assert "initial-hcloud-token" not in fake.servers[0].user_data
    # The create path prints no user-data regardless of the MC-only tls archive.
    out = capsys.readouterr().out
    assert fake.servers[0].user_data not in out


def test_production_mc_dry_run_redacts_reversible_tls_archive(tmp_path, capsys):
    source = _production_mc_settings(tmp_path)
    certificate = Path(source.hetzner_broker_client_certificate_file).read_text(encoding="utf-8")
    private_key = Path(source.hetzner_broker_client_key_file).read_text(encoding="utf-8")

    assert mc.main(_base_argv("--fqdn", "mc.example.com", "--dry-run"), settings=source) == 0
    out = capsys.readouterr().out
    assert certificate not in out and private_key not in out
    assert source.hetzner_broker_credential not in out
    assert source.secret_encryption_key not in out
    assert re.search(
        r"path: /opt/onebrain/mc-broker-tls\.tar\n"
        r"    permissions: '[0-7]+'\n"
        r"    encoding: gz\+b64\n"
        r"    content: \*\*\*REDACTED\*\*\*",
        out,
    )


@pytest.mark.parametrize(
    ("extra_args", "overrides", "marker"),
    [
        (("--fqdn", "mc_bad.example.com"), {}, "RFC1123"),
        (("--fleet-public-url", "https://other.example.com"), {}, "hostname must exactly match"),
        ((), {"fleet_dns_provider": ""}, "FLEET_DNS_PROVIDER=hetzner"),
        ((), {"hetzner_firewall_id": ""}, "HETZNER_FIREWALL_ID"),
    ],
)
def test_production_mc_bootstrap_requires_public_dns_and_precreated_firewall(
    tmp_path, extra_args, overrides, marker,
):
    source = _production_mc_settings(tmp_path, **overrides)
    with pytest.raises(ValueError, match=marker):
        mc.build_mc_artifacts(
            _args(_base_argv("--fqdn", "mc.example.com", *extra_args)),
            source,
        )


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
    assert "ONEBRAIN_IS_OPERATOR_SURFACE=true" in _asset_text(
        server.user_data, "/opt/onebrain/env/onebrain-api.env")
    assert server.firewall_ids and server.firewall_ids[-1] == "fw_1"
    # Secret hygiene: the create path prints ONLY the shape — the cloud-init (with every
    # baked secret) is NEVER dumped, and a real baked secret never appears in the capture.
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert server.user_data not in text
    baked_pw = re.search(
        r"POSTGRES_PASSWORD=([A-Za-z0-9_-]{20,})",
        extract_cloud_init_file(server.user_data, "/opt/onebrain/.env"),
    ).group(1)
    assert baked_pw not in text


def test_create_stamps_constant_fleet_label_on_mc_server():
    # The MC box carries the constant fleet label (counted by the cost cap) alongside its
    # deployment_id + role, so it can never be accidentally created twice or run uncapped.
    settings = _mc_settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                            hetzner_api_token="tok", hetzner_volume_size_gb=0)
    fake = FakeHetznerClient()
    assert mc.main(_base_argv("--no-dry-run"), settings=settings, client=fake) == 0
    labels = fake.servers[0].labels
    assert labels["managed-by"] == "onebrain-fleet"
    assert labels["deployment_id"] == "mc" and labels["role"] == "operator"


def test_bootstrap_mc_self_guard_reuses_existing_mc_and_does_not_recreate(capsys):
    # SELF-GUARD: if an MC box already exists, the broker's idempotency gate reuses it and the
    # runner says so plainly — it must NOT create a second MC (and must not reprint a login for
    # a box it never made).
    from app.provisioning.hetzner.client import (
        FLEET_LABEL_KEY,
        FLEET_LABEL_VALUE,
        ServerCreateRequest,
    )

    settings = _mc_settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                            hetzner_api_token="tok", hetzner_volume_size_gb=0)
    fake = FakeHetznerClient()
    # Pre-seed an existing MC box (deployment_id=mc + the fleet label).
    fake.create_server(ServerCreateRequest(
        name="onebrain-mc", server_type="cx23", image="ubuntu-24.04", location="nbg1",
        user_data="#cloud-config",
        labels={"deployment_id": "mc", "role": "operator", FLEET_LABEL_KEY: FLEET_LABEL_VALUE}))
    assert fake.calls.count("create_server") == 1

    rc = mc.main(_base_argv("--no-dry-run"), settings=settings, client=fake)

    assert rc == 0
    # NO second MC box created — the idempotency gate reused the existing one.
    assert fake.calls.count("create_server") == 1
    assert len(fake.list_servers("deployment_id=mc")) == 1
    out = capsys.readouterr().out
    assert "MC already exists (server_id=server_1 ip=203.0.113.1)" in out
    assert "reused, NOT recreated" in out
    # The "created" line (which would imply a fresh box) is NOT printed...
    assert "MC box created" not in out
    # ...nor is the admin login SURFACED (the freshly generated password was never applied to
    # the live box). The runbook's help text mentions the phrase generically, so assert the
    # actual credential line — the admin email after "admin login:" — is absent.
    assert "admin login: mc-admin@example.com" not in out
    assert "mc-admin@example.com" not in out


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
    assert "mc.onlyonebrain.com {" in _asset_text(art.cloud_init, "/opt/onebrain/Caddyfile")
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


def test_images_json_accepts_at_file(tmp_path):
    # `--images-json @path` reads the map from a file (the robust way to pass it through
    # PowerShell, which strips inline double quotes when calling a native exe). Parsed
    # identically to the inline form; a missing file fails closed with a clear ValueError.
    imgs = {m: f"ghcr.io/proark1/{m}@{_D}" for m in mc._MC_MODULES}
    p = tmp_path / "imgs.json"
    p.write_text(json.dumps(imgs), encoding="utf-8")
    assert mc._parse_images("@" + str(p)) == imgs                 # @file == inline
    assert mc._parse_images(json.dumps(imgs)) == imgs
    with pytest.raises(ValueError, match="@file could not be read"):
        mc._parse_images("@" + str(tmp_path / "does-not-exist.json"))


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
    assert "ONEBRAIN_ADMIN_EMAIL=${ONEBRAIN_ADMIN_EMAIL}" in _asset_text(
        art.cloud_init, "/opt/onebrain/env/onebrain-api.env")
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
    baked_pw = re.search(
        r"ONEBRAIN_ADMIN_PASSWORD=([A-Za-z0-9_-]{20,})",
        extract_cloud_init_file(server.user_data, "/opt/onebrain/.env"),
    ).group(1)
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

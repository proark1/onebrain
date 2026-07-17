"""P4-03: the HetznerProvisioner executor + the provisioning router backend
switch. NO live call anywhere — every test runs against FakeHetznerClient behind
InProcessHetznerBroker; the router switch injects the fake via a monkeypatched
build_hetzner_broker. (P4-04 appends the owner-OTP minting test.)"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.routers.provisioning as provisioning_router
from app.config import Settings
from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.rollout_exec import resolve_railway_target, target_provider
from app.fleet.memory import MemoryFleetStore
from app.provisioning.hetzner.broker import InProcessHetznerBroker
from app.provisioning.hetzner.fake import FakeHetznerClient
from app.provisioning.hetzner.provisioner import HetznerProvisioner
from app.provisioning.runs import (
    STATUS_DISPATCH_FAILED,
    STATUS_DISPATCHED,
    MemoryProvisioningRunStore,
    ProvisioningCallback,
    ProvisioningRun,
    apply_callback,
    create_run,
)

_DIGEST = "b" * 64
_MODULES = ("onebrain-api", "onebrain-admin-ui", "onebrain-workers")
_IMAGES = {m: f"ghcr.io/proark1/{m}@sha256:{_DIGEST}" for m in _MODULES}


def _settings(**over):
    data = dict(
        provisioner_backend="hetzner",
        hetzner_api_token="tok",
        hetzner_firewall_id="fw1",
        hetzner_allow_inprocess_broker=True,   # A6: required for in-process hetzner in tests
        hetzner_location="nbg1",
        hetzner_server_type="cx22",
        hetzner_image="ubuntu-24.04",
        hetzner_volume_size_gb=10,
        fleet_dns_provider="",
        fleet_base_domain="",
        fleet_dns_zone_id="",
        fleet_url="https://mc.example",
    )
    data.update(over)
    return Settings(**data)


def _control(dep="dep_a", modules=_MODULES, version="0.1.0", images=None, environment="production"):
    images = _IMAGES if images is None else images
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id=dep, customer_name="Customer A", account_id="acct_a",
        environment=environment, release_ring="pilot", current_version=version,
    ))
    for module_id in modules:
        store.upsert_module(DeploymentModule(dep, module_id, "0.1.0"))
    store.create_release(ReleaseManifest(
        version=version, git_sha="abc123", modules={m: "0.1.0" for m in modules},
        images=images, rollback_kind="code_only",
    ))
    return store


def _plain_run(dep="dep_a", version="0.1.0"):
    return ProvisioningRun(
        id="prun_direct", account_id="acct_a", deployment_id=dep,
        module_ids=("assistant", "communication"), requested_by="usr_op",
        request_payload={"initial_version": version},
    )


def _run(prov, dep="dep_a", version="0.1.0"):
    return create_run(prov, account_id="acct_a", deployment_id=dep, module_ids=["assistant", "communication"],
                      requested_by="usr_op", payload={"initial_version": version})


def _wire_router(monkeypatch, settings, prov, control, fake, fleet=None):
    fleet = fleet if fleet is not None else MemoryFleetStore()
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: settings)
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_fleet_store", lambda: fleet)   # P5-03 bundle-assembly seam
    monkeypatch.setattr(provisioning_router, "build_hetzner_broker", lambda s: InProcessHetznerBroker(fake))


# --- direct executor ---------------------------------------------------------

def test_dispatch_creates_server_with_firewall_and_writes_d6_slots():
    control = _control()
    fake = FakeHetznerClient()
    out = HetznerProvisioner(_settings(), InProcessHetznerBroker(fake), control).dispatch(_plain_run())

    assert len(fake.servers) == 1
    server = fake.servers[0]
    assert server.firewall_ids == ("fw1",)            # H-3: firewall attached IN the create call
    assert server.location == "nbg1"                  # EU region
    assert server.user_data.startswith("#cloud-config") or server.user_data  # non-empty rendered cloud-init
    assert out.external_provider == "hetzner"
    assert out.railway_project_id == "hetzner:server_1"
    assert out.railway_environment_id == "onebrain-dep-a"
    assert out.result_payload["service_ids"] == {m: m for m in _MODULES}
    assert out.status == STATUS_DISPATCHED


def test_dispatch_bakes_separate_release_verifiers_for_dev_and_customer_boxes():
    settings = _settings(
        release_verify_public_key="production-release-public-key",
        dev_release_verify_public_key="development-release-public-key",
    )
    production_fake = FakeHetznerClient()
    HetznerProvisioner(settings, InProcessHetznerBroker(production_fake), _control()).dispatch(_plain_run())
    assert "UPDATE_RELEASE_PUBLIC_KEY=production-release-public-key" in production_fake.servers[0].user_data
    assert "development-release-public-key" not in production_fake.servers[0].user_data

    development_fake = FakeHetznerClient()
    development = _control(dep="dev-gate", environment="development")
    HetznerProvisioner(settings, InProcessHetznerBroker(development_fake), development).dispatch(
        _plain_run(dep="dev-gate")
    )
    assert "UPDATE_RELEASE_PUBLIC_KEY=development-release-public-key" in development_fake.servers[0].user_data
    assert "production-release-public-key" not in development_fake.servers[0].user_data


def test_development_dispatch_fails_before_server_creation_without_dev_key():
    fake = FakeHetznerClient()
    control = _control(dep="dev-gate", environment="development")

    with pytest.raises(RuntimeError, match="DEV_RELEASE_VERIFY_PUBLIC_KEY"):
        HetznerProvisioner(_settings(), InProcessHetznerBroker(fake), control).dispatch(
            _plain_run(dep="dev-gate")
        )

    assert fake.servers == []


def test_dispatch_stamps_constant_fleet_label_on_server():
    # Every provisioned box carries the constant fleet label (so the cost cap counts it) in
    # ADDITION to its deployment_id (the idempotency key).
    control = _control()
    fake = FakeHetznerClient()
    HetznerProvisioner(_settings(), InProcessHetznerBroker(fake), control).dispatch(_plain_run())
    labels = fake.servers[0].labels
    assert labels["managed-by"] == "onebrain-fleet"
    assert labels["deployment_id"] == "dep_a"


def test_dispatch_is_idempotent_one_server_per_deployment():
    # COST SAFETY: dispatching TWICE for the same deployment must create exactly ONE server
    # (and one volume) — a retry/double-dispatch/replayed callback reuses the existing box.
    control = _control()
    fake = FakeHetznerClient()
    prov = HetznerProvisioner(_settings(hetzner_volume_size_gb=10), InProcessHetznerBroker(fake), control)

    first = prov.dispatch(_plain_run())
    second = prov.dispatch(_plain_run())

    assert fake.calls.count("create_server") == 1        # exactly one server despite two dispatches
    assert fake.calls.count("create_volume") == 1        # ...and no duplicate volume
    assert first.railway_project_id == "hetzner:server_1"
    assert second.railway_project_id == first.railway_project_id     # the reused server id
    assert second.external_run_url == first.external_run_url
    assert second.status == STATUS_DISPATCHED


def test_dispatch_attaches_data_volume():
    control = _control()
    fake = FakeHetznerClient()
    out = HetznerProvisioner(_settings(hetzner_volume_size_gb=10),
                             InProcessHetznerBroker(fake), control).dispatch(_plain_run())

    assert fake.calls.index("create_volume") < fake.calls.index("create_server")
    assert "vol_1" in fake.servers[0].volume_ids       # attached in-create (H-3)
    assert out.result_payload["erasure_manifest"]["volume_ids"] == ["vol_1"]


def test_dispatch_skips_dns_without_provider():
    control = _control()
    fake = FakeHetznerClient()
    out = HetznerProvisioner(_settings(fleet_dns_provider="", fleet_base_domain=""),
                             InProcessHetznerBroker(fake), control).dispatch(_plain_run())

    assert "upsert_dns_record" not in fake.calls
    assert out.external_run_url == "203.0.113.1"        # falls back to the raw IP


def test_dispatch_sets_dns_with_provider():
    control = _control()
    fake = FakeHetznerClient()
    out = HetznerProvisioner(
        _settings(fleet_dns_provider="hetzner", fleet_base_domain="fleet.example", fleet_dns_zone_id="z1"),
        InProcessHetznerBroker(fake), control,
    ).dispatch(_plain_run())

    assert fake.calls[-1] == "upsert_dns_record"        # DNS last
    assert fake.dns[0].name == "dep-a"                   # zone-relative LABEL, not the full fqdn
    assert fake.dns[0].ipv4 == "203.0.113.1"            # broker filled the empty ipv4 from the server IP
    assert out.external_run_url == "dep-a.fleet.example"   # ...but the box hostname is still the full fqdn
    assert out.result_payload["erasure_manifest"]["dns_record_id"] == "dns_1"


def test_provider_names_are_hostname_safe_and_length_bounded():
    deployment_id = "customer_" + "long_name_" * 12
    control = _control(dep=deployment_id)
    fake = FakeHetznerClient()
    out = HetznerProvisioner(
        _settings(fleet_dns_provider="hetzner", fleet_base_domain="fleet.example", fleet_dns_zone_id="z1"),
        InProcessHetznerBroker(fake), control,
    ).dispatch(_plain_run(dep=deployment_id))

    assert len(fake.servers[0].name) <= 63
    assert "_" not in fake.servers[0].name
    assert len(fake.dns[0].name) <= 63
    assert "_" not in fake.dns[0].name
    assert out.external_run_url == f"{fake.dns[0].name}.fleet.example"


# --- fail-closed paths (through the router helper) ----------------------------

def test_dispatch_fails_closed_without_signed_images(monkeypatch):
    control = _control(images={})     # release exists but carries no digest-pinned images
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    _wire_router(monkeypatch, _settings(), prov, control, fake)

    out = provisioning_router._dispatch_run(_run(prov))
    assert out.status == STATUS_DISPATCH_FAILED
    assert "digest-pinned images" in out.failure_reason
    assert fake.servers == []          # never reached the broker


def test_dispatch_maps_api_error_to_dispatch_failed(monkeypatch):
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient(fail_on={"create_server"})
    _wire_router(monkeypatch, _settings(secret_encryption_key="unit-test-secret-key"), prov, control, fake)

    # owner_otp + owner_email threaded (G3-3) so bundle assembly (which runs before the broker)
    # passes and the run reaches the failing broker create.
    out = provisioning_router._dispatch_run(_run(prov), owner_otp="owner-otp",
                                            owner_email="owner@example.com")   # must NOT raise
    assert out.status == STATUS_DISPATCH_FAILED
    assert "Hetzner API error" in out.failure_reason


def test_dispatch_maps_missing_runtime_asset_to_dispatch_failed(monkeypatch):
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    _wire_router(monkeypatch, _settings(), prov, control, fake)
    monkeypatch.setattr(
        HetznerProvisioner,
        "dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing template")),
    )

    out = provisioning_router._dispatch_run(_run(prov))

    assert out.status == STATUS_DISPATCH_FAILED
    assert out.failure_reason == "missing template"
    assert fake.servers == []


def test_api_image_packages_cloud_init_assets():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY deploy ./deploy" in dockerfile


def test_dispatch_run_router_switch_selects_backend(monkeypatch):
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    _wire_router(monkeypatch, _settings(secret_encryption_key="unit-test-secret-key"), prov, control, fake)

    # hetzner backend -> the hetzner executor ran (owner_otp + owner_email threaded so the
    # bundle is valid).
    out = provisioning_router._dispatch_run(_run(prov), owner_otp="owner-otp",
                                            owner_email="owner@example.com")
    assert out.external_provider == "hetzner" and out.status == STATUS_DISPATCHED
    assert len(fake.servers) == 1

    # github backend -> GitHubWorkflowDispatcher (unchanged); unconfigured here so it
    # dispatch-fails with a GitHub reason, and the hetzner fake is NOT invoked.
    monkeypatch.setattr(provisioning_router, "get_settings",
                        lambda: _settings(provisioner_backend="github"))
    gh_out = provisioning_router._dispatch_run(_run(prov))
    assert gh_out.status == STATUS_DISPATCH_FAILED
    assert "GitHub" in gh_out.failure_reason
    assert len(fake.servers) == 1      # hetzner path was not taken

    # unknown backend -> named fail-closed reason, never a silent fallback.
    monkeypatch.setattr(provisioning_router, "get_settings",
                        lambda: _settings(provisioner_backend="bogus"))
    bad = provisioning_router._dispatch_run(_run(prov))
    assert bad.status == STATUS_DISPATCH_FAILED
    assert "unknown provisioner_backend: bogus" in bad.failure_reason


def test_dispatch_requires_configuration():
    control = _control()
    # provisioner_backend defaults to github -> enabled is False -> refuses.
    prov = HetznerProvisioner(_settings(provisioner_backend="github"),
                              InProcessHetznerBroker(FakeHetznerClient()), control)
    assert prov.enabled is False
    with pytest.raises(RuntimeError, match="not configured"):
        prov.dispatch(_plain_run())


# --- D-6 slot contract end-to-end --------------------------------------------

def test_resolve_target_classifies_hetzner_run():
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    dispatched = HetznerProvisioner(_settings(), InProcessHetznerBroker(fake), control).dispatch(_run(prov))
    prov.update_run(dispatched)

    # The box's succeeded callback round-trips the D-6 coordinates + service_ids
    # (pins the slot contract end-to-end); resolve_railway_target reads them back.
    apply_callback(prov, _settings(), dispatched.id, ProvisioningCallback(
        status="succeeded",
        railway_project_id=dispatched.railway_project_id,
        railway_environment_id=dispatched.railway_environment_id,
        result_payload={"service_ids": dispatched.result_payload["service_ids"]},
        smoke_status="passed",
    ))

    target = resolve_railway_target(prov, "dep_a")
    assert target["railway_project_id"] == "hetzner:server_1"
    assert target["railway_environment_id"] == "onebrain-dep-a"
    assert target["service_ids"] == {m: m for m in _MODULES}
    assert target_provider(target) == "hetzner"


def test_resolve_target_survives_box_callback_that_omits_d6_coordinates():
    """P5 regression: a LIVE Hetzner box's succeeded callback reports only
    status/smoke_status/bootstrap_password/external_run_url (see the done_cb in
    app/provisioning/hetzner/render.py) — it does NOT echo the D-6 coordinates that
    dispatch wrote. apply_callback must preserve them (unlike the pre-P5 unconditional
    overwrite, which wiped them to ""), or resolve_railway_target finds no truthy
    railway_project_id and the box can never be targeted for a pull-update. The
    Phase-4 test above sidesteps this by re-sending the coordinates in the callback;
    this one deliberately does NOT."""
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    settings = _settings(secret_encryption_key="unit-test-secret-key")
    dispatched = HetznerProvisioner(settings, InProcessHetznerBroker(fake), control).dispatch(_run(prov))
    prov.update_run(dispatched)

    # Exactly what the box posts — no railway_project_id / railway_environment_id /
    # result_payload. With the old unconditional overwrite these wiped to "".
    applied = apply_callback(prov, settings, dispatched.id, ProvisioningCallback(
        status="succeeded",
        smoke_status="passed",
        bootstrap_password="owner-otp",
        external_run_url="203.0.113.1",
    ))
    # The persisted run kept the dispatch-written coordinates AND the erasure manifest
    # (the teardown ids), not just the piece resolve_railway_target happens to read.
    assert applied.railway_project_id == "hetzner:server_1"
    assert applied.railway_environment_id == "onebrain-dep-a"
    assert applied.result_payload["service_ids"] == {m: m for m in _MODULES}
    assert applied.result_payload["erasure_manifest"]["server_id"] == "server_1"

    target = resolve_railway_target(prov, "dep_a")
    assert target["railway_project_id"] == "hetzner:server_1"
    assert target["railway_environment_id"] == "onebrain-dep-a"
    assert target["service_ids"] == {m: m for m in _MODULES}
    assert target_provider(target) == "hetzner"


# --- P4-04: owner one-time password minting (H-10/A8) ------------------------

def test_owner_otp_minted_hash_only_and_flagged():
    from app.auth.passwords import verify_password
    from app.platform.memory import MemoryPlatformStore
    from app.provisioning.hetzner.provisioner import store_owner_one_time_password
    from app.provisioning.service import CustomerProvisioner
    from app.users.memory import MemoryUserStore

    platform = MemoryPlatformStore()
    control = MemoryControlPlaneStore()
    users = MemoryUserStore()

    result = CustomerProvisioner(platform, control, None, users).provision(
        account_id="acct_owner", account_kind="organization", customer_name="Owner Co",
        owner_user_id="usr_op", module_ids=[], deployment_id="dep_owner",
        deployment_type="dedicated_railway", region="", release_ring="pilot",
        initial_version="0.1.0", owner_email="Owner@Example.com",
    )

    # Plaintext OTP returned exactly once.
    otp = result.owner_one_time_password
    assert otp

    # The owner User: admin, must_change flagged, tenant = account, hash matches.
    owner = users.get_by_email("owner@example.com")
    assert owner is not None
    assert owner.role_id == "admin"
    assert owner.must_change_password is True
    assert owner.tenant_id == "acct_owner"
    assert verify_password(otp, owner.password_hash)
    # NEVER persisted in plaintext — only the hash is stored.
    assert owner.password_hash != otp
    assert otp not in owner.password_hash

    # The run's bootstrap_secret_id points at a stored owner-OTP envelope, recorded
    # in the erasure manifest; the envelope holds ciphertext, not the plaintext.
    prov = MemoryProvisioningRunStore()
    run = _run(prov, dep="dep_owner")
    settings = Settings(secret_encryption_key="unit-test-secret-key")
    updated = store_owner_one_time_password(prov, settings, run, otp)
    assert updated.bootstrap_secret_id
    envelope = prov.get_secret(updated.bootstrap_secret_id)
    assert envelope.purpose == "owner_one_time_password"
    assert updated.result_payload["erasure_manifest"]["secret_ids"] == [envelope.id]
    assert otp not in envelope.ciphertext

    # No owner_email -> no owner minted, no OTP (today's behavior, dormant).
    plain = CustomerProvisioner(MemoryPlatformStore(), MemoryControlPlaneStore(), None, users).provision(
        account_id="acct_none", account_kind="organization", customer_name="No Owner",
        owner_user_id="usr_op", module_ids=[], deployment_id="dep_none",
        deployment_type="dedicated_railway", region="", release_ring="pilot", initial_version="0.1.0",
    )
    assert plain.owner_one_time_password == ""
    assert store_owner_one_time_password(prov, settings, _run(prov, dep="dep_none"), "").bootstrap_secret_id == ""


# --- P5-03: box secret-bundle + bootstrap-token assembly (G3-3) ---------------

def _p5_settings(**over):
    return _settings(secret_encryption_key="unit-test-secret-key", **over)


def _open_bundle(prov, settings, dep="dep_a") -> dict:
    import json
    from app.provisioning.runs import OneTimeSecretCipher
    return json.loads(OneTimeSecretCipher(settings).open_bundle(prov.get_secret_bundle(dep).ciphertext))


def test_dispatch_mints_bundle_and_unconsumed_bootstrap_token():
    control = _control()
    prov = MemoryProvisioningRunStore()
    fleet = MemoryFleetStore()
    fake = FakeHetznerClient()
    settings = _p5_settings()
    out = HetznerProvisioner(settings, InProcessHetznerBroker(fake), control,
                             prov_store=prov, fleet_store=fleet).dispatch(
        _run(prov), owner_otp="owner-otp", service_key="sk_abc", space_id="space_1",
        owner_email="Owner@Example.com")
    assert out.status == STATUS_DISPATCHED

    # The re-readable bundle is stored with the THREADED owner OTP + service key + space
    # id (G3-3) and the minted foundational secrets + fleet key.
    bundle = prov.get_secret_bundle("dep_a")
    assert bundle is not None and bundle.secrets_epoch == 0
    body = _open_bundle(prov, settings)
    # The admin is loginable: BOTH the email and the password are baked. The email is the
    # threaded owner_email (normalized to match the box's seed-time .strip().lower()).
    assert body["ONEBRAIN_ADMIN_EMAIL"] == "owner@example.com"
    assert body["ONEBRAIN_ADMIN_PASSWORD"] == "owner-otp"
    assert body["ONEBRAIN_SERVICE_KEY"] == "sk_abc"
    assert body["ONEBRAIN_SPACE_ID"] == "space_1"
    assert body["POSTGRES_PASSWORD"] and body["REDIS_PASSWORD"] and body["UPDATE_BACKUP_KEY"]
    assert body["ONEBRAIN_FLEET_KEY"].startswith("fk_")
    # The minted fleet key is registered (box heartbeat + rotation re-fetch auth).
    assert [k.deployment_id for k in fleet.list_keys("dep_a")] == ["dep_a"]

    # The raw first-boot token is baked into user-data, and its hash is stored UNCONSUMED.
    import re
    from app.fleet.keys import hash_secret, parse_bootstrap_token
    raw = re.search(r"ONEBRAIN_BOOTSTRAP_TOKEN=(bt_[A-Za-z0-9_-]+)", fake.servers[0].user_data)
    assert raw, "bootstrap token must be baked in box.env"
    record = prov.get_bootstrap_token(hash_secret(parse_bootstrap_token(raw.group(1))[1]))
    assert record is not None and not record.consumed_at and record.deployment_id == "dep_a"


def test_full_stack_dispatch_seals_distinct_app_credentials_and_bootstrap_descriptor():
    from app.servicekeys.base import generate_key
    from tests.boot_config_helper import extract_cloud_init_file

    modules = (
        "onebrain-api", "onebrain-admin-ui", "onebrain-workers", "assistant-service",
        "communication-api", "communication-widget", "communication-voice",
        "communication-workers",
    )
    images = {module: f"ghcr.io/proark1/{module}@sha256:{_DIGEST}" for module in modules}
    control = _control(modules=modules, images=images)
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    settings = _p5_settings()
    assistant_key = generate_key()[2]
    communication_key = generate_key()[2]

    out = HetznerProvisioner(
        settings, InProcessHetznerBroker(fake), control,
        prov_store=prov, fleet_store=MemoryFleetStore(),
    ).dispatch(
        _run(prov), owner_otp="owner-otp", owner_email="owner@example.com",
        integration_credentials={
            "assistant": (assistant_key, "sp_assistant"),
            "communication": (communication_key, "sp_communication"),
        },
    )

    assert out.status == STATUS_DISPATCHED
    bundle = _open_bundle(prov, settings)
    assert bundle["ONEBRAIN_ASSISTANT_SERVICE_KEY"] == assistant_key
    assert bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"] == communication_key
    assert bundle["ONEBRAIN_COMMUNICATION_SPACE_ID"] == "sp_communication"
    assert assistant_key != communication_key
    api_env = extract_cloud_init_file(fake.servers[0].user_data, "/opt/onebrain/env/onebrain-api.env")
    assert "ONEBRAIN_CUSTOMER_BOOTSTRAP=" in api_env
    assert "ONEBRAIN_ASSISTANT_SERVICE_KEY=${ONEBRAIN_ASSISTANT_SERVICE_KEY}" in api_env
    assert "ONEBRAIN_COMMUNICATION_SERVICE_KEY=${ONEBRAIN_COMMUNICATION_SERVICE_KEY}" in api_env


def test_dispatch_fails_closed_on_invalid_bundle(monkeypatch):
    # No owner email/OTP -> ONEBRAIN_ADMIN_EMAIL + ONEBRAIN_ADMIN_PASSWORD empty ->
    # validate_bundle fails -> the run dispatch-fails and NO server is created (never
    # provision a box that can't seed a loginable admin / can't come up).
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    _wire_router(monkeypatch, _p5_settings(), prov, control, fake)
    out = provisioning_router._dispatch_run(_run(prov))   # owner_otp + owner_email omitted
    assert out.status == STATUS_DISPATCH_FAILED
    assert "secret bundle invalid" in out.failure_reason
    assert fake.servers == []


def test_dispatch_fails_closed_when_active_signer_excluded(monkeypatch):
    # G1-1: refuse to ship a bundle whose accepted wrapper-key set excludes MC's active
    # desired-state signer (that set would strand the box at envelope_signature_invalid).
    from app.trust.signing import generate_keypair
    priv, pub = generate_keypair()
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    _wire_router(monkeypatch, _p5_settings(fleet_desired_state_private_key=priv,
                                           fleet_desired_state_public_keys="someone-else"),
                 prov, control, fake)
    out = provisioning_router._dispatch_run(_run(prov), owner_otp="owner-otp",
                                            owner_email="owner@example.com")
    assert out.status == STATUS_DISPATCH_FAILED
    assert "active_signer_not_in_public_key_set" in out.failure_reason
    assert fake.servers == []

    # Adding the active signer's derived public key to the set unblocks the provision.
    _wire_router(monkeypatch, _p5_settings(fleet_desired_state_private_key=priv,
                                           fleet_desired_state_public_keys=f"someone-else,{pub}"),
                 prov, control, fake)
    ok = provisioning_router._dispatch_run(_run(prov), owner_otp="owner-otp",
                                           owner_email="owner@example.com")
    assert ok.status == STATUS_DISPATCHED
    assert _open_bundle(prov, _p5_settings())["UPDATE_DESIRED_STATE_PUBLIC_KEYS"] == f"someone-else,{pub}"


def test_retry_reuses_stored_bundle_without_reminting():
    # A retry (bundle already present for the deployment) REUSES it — the owner OTP baked
    # into it is never re-minted — and only mints a fresh single-use token.
    control = _control()
    prov = MemoryProvisioningRunStore()
    fleet = MemoryFleetStore()
    fake = FakeHetznerClient()
    p = HetznerProvisioner(_p5_settings(), InProcessHetznerBroker(fake), control,
                           prov_store=prov, fleet_store=fleet)
    p.dispatch(_run(prov), owner_otp="owner-otp", service_key="sk1", space_id="sp1",
               owner_email="owner@example.com")
    ct1 = prov.get_secret_bundle("dep_a").ciphertext
    keys_after_first = len(fleet.list_keys("dep_a"))

    p.dispatch(_run(prov), owner_otp="")   # retry: no fresh OTP
    assert prov.get_secret_bundle("dep_a").ciphertext == ct1        # bundle preserved (OTP intact)
    assert len(fleet.list_keys("dep_a")) == keys_after_first        # no duplicate fleet key


# --- P5-05: default-deny Cloud Firewall + DNS provider gating -----------------

def _p5_dispatch(settings, fake, *, prov=None, fleet=None):
    prov = prov if prov is not None else MemoryProvisioningRunStore()
    fleet = fleet if fleet is not None else MemoryFleetStore()
    p = HetznerProvisioner(settings, InProcessHetznerBroker(fake), _control(),
                           prov_store=prov, fleet_store=fleet)
    # owner_email is REQUIRED now (ONEBRAIN_ADMIN_EMAIL is a required bundle key), so a
    # bundle-building dispatch must thread it or validate_bundle fails closed.
    return p.dispatch(_run(prov), owner_otp="owner-otp", owner_email="owner@example.com")


def test_provision_creates_default_deny_firewall_no_ssh_by_default():
    fake = FakeHetznerClient()
    out = _p5_dispatch(_p5_settings(hetzner_firewall_id=""), fake)   # no pre-created firewall
    assert len(fake.firewalls) == 1
    ports = sorted(r.port for r in fake.firewalls[0].rules)
    assert ports == ["443", "80"]                       # exactly inbound tcp 80 + 443...
    assert "22" not in ports                             # ...NO inbound ssh by default
    assert "5432" not in ports and "6379" not in ports  # Postgres/Redis internet-unreachable
    assert all(r.direction == "in" for r in fake.firewalls[0].rules)
    # the created firewall id is recorded in the erasure manifest for teardown.
    assert out.result_payload["erasure_manifest"]["firewall_id"] == "fw_1"


def test_provision_firewall_allows_ssh_only_under_break_glass_flag():
    fake = FakeHetznerClient()
    _p5_dispatch(_p5_settings(hetzner_firewall_id="", hetzner_firewall_allow_ssh=True), fake)
    assert "22" in sorted(r.port for r in fake.firewalls[0].rules)


def test_provision_attaches_precreated_firewall_without_creating():
    fake = FakeHetznerClient()
    _p5_dispatch(_p5_settings(hetzner_firewall_id="fw_existing"), fake)
    assert fake.firewalls == []                          # nothing created
    assert fake.servers[0].firewall_ids == ("fw_existing",)   # attached in-create


def test_provision_dns_skipped_for_non_hetzner_provider():
    # A cloudflare/unknown provider -> DNS skipped (serve on IP), never mis-called.
    fake = FakeHetznerClient()
    out = _p5_dispatch(_p5_settings(fleet_dns_provider="cloudflare", fleet_base_domain="fleet.example",
                                    fleet_dns_zone_id="z1"), fake)
    assert fake.dns == []
    assert out.external_run_url == "203.0.113.1"         # the raw server IP


def test_provision_dns_upserted_for_hetzner_provider():
    fake = FakeHetznerClient()
    out = _p5_dispatch(_p5_settings(fleet_dns_provider="hetzner", fleet_base_domain="fleet.example",
                                    fleet_dns_zone_id="z1"), fake)
    # The A record posts the zone-relative LABEL (Hetzner appends the zone) — NOT the full
    # fqdn, which would resolve as "dep-a.fleet.example.fleet.example".
    assert len(fake.dns) == 1 and fake.dns[0].name == "dep-a"
    assert out.external_run_url == "dep-a.fleet.example"   # the box hostname stays the full fqdn


def test_provision_records_hetzner_backups_in_manifest():
    # BK2: the broker's Hetzner-Backups state (root-disk only) lands in the erasure manifest.
    def _dispatch(enable):
        fake = FakeHetznerClient()
        prov = MemoryProvisioningRunStore()
        p = HetznerProvisioner(_p5_settings(), InProcessHetznerBroker(fake, enable_backups=enable),
                               _control(), prov_store=prov, fleet_store=MemoryFleetStore())
        return fake, p.dispatch(_run(prov), owner_otp="owner-otp", owner_email="owner@example.com")

    fake_on, out_on = _dispatch(True)
    assert "enable_backup" in fake_on.calls
    assert out_on.result_payload["erasure_manifest"]["hetzner_backups"] is True

    fake_off, out_off = _dispatch(False)
    assert "enable_backup" not in fake_off.calls
    assert out_off.result_payload["erasure_manifest"]["hetzner_backups"] is False


def test_bundle_carries_backup_s3_credentials_from_settings():
    # BK3: the shared fleet S3 credential (from settings) is SEALED into the box bundle (never
    # user-data). Empty when unset (optional key, backups off).
    def _dispatch(**over):
        fake, prov = FakeHetznerClient(), MemoryProvisioningRunStore()
        settings = _p5_settings(**over)
        HetznerProvisioner(settings, InProcessHetznerBroker(fake), _control(),
                           prov_store=prov, fleet_store=MemoryFleetStore()).dispatch(
                               _run(prov), owner_otp="owner-otp", owner_email="owner@example.com")
        return _open_bundle(prov, settings)

    b = _dispatch(backup_object_store_access_key="AKIA-fleet", backup_object_store_secret_key="s3cr3t")
    assert b["ONEBRAIN_BACKUP_S3_ACCESS_KEY"] == "AKIA-fleet"
    assert b["ONEBRAIN_BACKUP_S3_SECRET_KEY"] == "s3cr3t"
    off = _dispatch()
    assert off["ONEBRAIN_BACKUP_S3_ACCESS_KEY"] == "" and off["ONEBRAIN_BACKUP_S3_SECRET_KEY"] == ""


# --- G1-7: customer-box provisioning callback authenticates via the per-run token -----

def test_customer_box_callback_authenticates_with_per_run_token(monkeypatch):
    # A customer Hetzner box bakes a PER-RUN ONEBRAIN_PROVISIONING_CALLBACK_TOKEN that cannot
    # match MC's single global callback hash. MC stores the per-run hash on the run and accepts
    # the box's done_cb/fail_cb against it, so the metadata-egress failure_reason is not lost
    # to a 401 (which would degrade it to a generic timeout).
    import re

    from fastapi import HTTPException

    from app.provisioning.runs import hash_callback_secret

    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    # A DIFFERENT global callback hash is configured (the Railway path); the box's per-run
    # token deliberately does not match it.
    settings = _p5_settings(provisioning_callback_key_hash=hash_callback_secret("global-secret"),
                            provisioning_callback_key_id="cb_global")
    _wire_router(monkeypatch, settings, prov, control, fake)

    dispatched = provisioning_router._dispatch_run(_run(prov), owner_otp="owner-otp",
                                                   owner_email="owner@example.com")
    assert dispatched.status == STATUS_DISPATCHED
    token = re.search(r"ONEBRAIN_PROVISIONING_CALLBACK_TOKEN=([A-Za-z0-9_-]+)",
                      fake.servers[0].user_data).group(1)
    assert token and token != "global-secret"

    # fail_cb: the box posts the baked per-run token and NO key-id header -> authenticated,
    # and the failure_reason survives.
    out = provisioning_router.provisioning_callback(
        dispatched.id,
        provisioning_router.ProvisioningCallbackIn(
            status="failed", smoke_status="failed", failure_reason="metadata_egress_block_failed"),
        authorization=f"Bearer {token}",
        x_onebrain_callback_key_id="",
    )
    assert out.status == "failed"
    assert out.failure_reason == "metadata_egress_block_failed"

    # A wrong bearer still 401s (it falls back to the global mechanism the box's token never
    # satisfies) — per-run acceptance is scoped to the exact minted token.
    with pytest.raises(HTTPException) as exc:
        provisioning_router.provisioning_callback(
            dispatched.id,
            provisioning_router.ProvisioningCallbackIn(status="failed"),
            authorization="Bearer wrong-token",
            x_onebrain_callback_key_id="",
        )
    assert exc.value.status_code == 401


def test_ssh_key_ids_accepts_names_and_ids():
    """break-glass SSH keys: the Hetzner API's ssh_keys field takes int ids OR key
    names (the console shows names), so _ssh_key_ids must pass names through and coerce
    numeric entries to ints — dropping only blanks."""
    from app.provisioning.hetzner.provisioner import _ssh_key_ids

    assert _ssh_key_ids("assaddar-voice-edge-hetzner-2026-07-09") == (
        "assaddar-voice-edge-hetzner-2026-07-09",)
    assert _ssh_key_ids("12345") == (12345,)
    assert _ssh_key_ids("12345, my-key ,") == (12345, "my-key")
    assert _ssh_key_ids("") == ()

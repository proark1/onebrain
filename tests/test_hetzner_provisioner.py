"""P4-03: the HetznerProvisioner executor + the provisioning router backend
switch. NO live call anywhere — every test runs against FakeHetznerClient behind
InProcessHetznerBroker; the router switch injects the fake via a monkeypatched
build_hetzner_broker. (P4-04 appends the owner-OTP minting test.)"""

from __future__ import annotations

import pytest

import app.routers.provisioning as provisioning_router
from app.config import Settings
from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.rollout_exec import resolve_railway_target, target_provider
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


def _control(dep="dep_a", modules=_MODULES, version="0.1.0", images=None):
    images = _IMAGES if images is None else images
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id=dep, customer_name="Customer A", account_id="acct_a",
        release_ring="pilot", current_version=version,
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
        bundle_id="full_stack", requested_by="usr_op",
        request_payload={"initial_version": version},
    )


def _run(prov, dep="dep_a", version="0.1.0"):
    return create_run(prov, account_id="acct_a", deployment_id=dep, bundle_id="full_stack",
                      requested_by="usr_op", payload={"initial_version": version})


def _wire_router(monkeypatch, settings, prov, control, fake):
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: settings)
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
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
    assert out.railway_environment_id == "onebrain-dep_a"
    assert out.result_payload["service_ids"] == {m: m for m in _MODULES}
    assert out.status == STATUS_DISPATCHED


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
    assert fake.dns[0].name == "dep_a.fleet.example"
    assert fake.dns[0].ipv4 == "203.0.113.1"            # broker filled the empty ipv4 from the server IP
    assert out.external_run_url == "dep_a.fleet.example"
    assert out.result_payload["erasure_manifest"]["dns_record_id"] == "dns_1"


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
    _wire_router(monkeypatch, _settings(), prov, control, fake)

    out = provisioning_router._dispatch_run(_run(prov))   # must NOT raise
    assert out.status == STATUS_DISPATCH_FAILED
    assert "Hetzner API error" in out.failure_reason


def test_dispatch_run_router_switch_selects_backend(monkeypatch):
    control = _control()
    prov = MemoryProvisioningRunStore()
    fake = FakeHetznerClient()
    _wire_router(monkeypatch, _settings(), prov, control, fake)

    # hetzner backend -> the hetzner executor ran.
    out = provisioning_router._dispatch_run(_run(prov))
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
    assert target["railway_environment_id"] == "onebrain-dep_a"
    assert target["service_ids"] == {m: m for m in _MODULES}
    assert target_provider(target) == "hetzner"

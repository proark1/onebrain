"""P4-01: the Hetzner client/broker seam. No live call anywhere — the real
`UrllibHetznerClient` is exercised only via an injected opener (request-SHAPE
assertions), and the broker runs against `FakeHetznerClient`."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.config import Settings
from app.provisioning.hetzner.broker import (
    BrokerProvisionResult,
    InProcessHetznerBroker,
    build_hetzner_broker,
)
from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    HetznerApiError,
    ServerCreateRequest,
    VolumeCreateRequest,
)
from app.provisioning.hetzner.fake import FakeHetznerClient
from app.provisioning.hetzner.urllib_client import UrllibHetznerClient


class _FakeResponse:
    def __init__(self, body: bytes = b""):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


_SERVER_OK = json.dumps(
    {"server": {"id": 12345, "status": "initializing", "public_net": {"ipv4": {"ip": "203.0.113.9"}}}}
).encode("utf-8")


def _server_req(**over) -> ServerCreateRequest:
    base = dict(
        name="onebrain-dep_a",
        server_type="cx22",
        image="ubuntu-24.04",
        location="nbg1",
        user_data="#cloud-config\n{}",
        ssh_key_ids=(7,),
        firewall_ids=(42,),
        volume_ids=(),
        labels={"deployment_id": "dep_a"},
    )
    base.update(over)
    return ServerCreateRequest(**base)


# --- real client (opener-injected; no network) -------------------------------

def test_urllib_client_builds_server_post():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data)
        return _FakeResponse(_SERVER_OK)

    client = UrllibHetznerClient("sentinel-token-XYZ", "dns-token", opener=opener)
    result = client.create_server(_server_req())

    assert captured["url"] == "https://api.hetzner.cloud/v1/servers"
    assert captured["method"] == "POST"
    # The token came from the CONSTRUCTOR arg, never a global.
    assert captured["auth"] == "Bearer sentinel-token-XYZ"
    body = captured["body"]
    assert body["location"] == "nbg1"
    assert body["user_data"] == "#cloud-config\n{}"
    assert body["firewalls"] == [{"firewall": 42}]   # H-3: attached IN the create call
    assert body["ssh_keys"] == [7]
    # a volume attached in-create shows up as `volumes`
    body2 = {}

    def opener2(request, timeout):
        body2.update(json.loads(request.data))
        return _FakeResponse(_SERVER_OK)

    UrllibHetznerClient("t", opener=opener2).create_server(_server_req(volume_ids=("31",)))
    assert body2["volumes"] == ["31"]

    assert result.server_id == "12345"
    assert result.public_ipv4 == "203.0.113.9"
    assert result.status == "initializing"


def test_urllib_client_maps_http_error():
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            "https://api.hetzner.cloud/v1/servers", 500, "err", None, io.BytesIO(b"boom-detail")
        )

    client = UrllibHetznerClient("t", opener=opener)
    with pytest.raises(HetznerApiError) as excinfo:
        client.create_server(_server_req())
    assert excinfo.value.status == 500
    assert "boom-detail" in excinfo.value.body
    assert len(excinfo.value.body) <= 500


def test_urllib_client_token_never_read_from_global(monkeypatch):
    # Even with a decoy token on the ambient settings, the client uses only its
    # constructor arg (it never imports get_settings).
    captured = {}

    def opener(request, timeout):
        captured["auth"] = request.headers.get("Authorization")
        return _FakeResponse(_SERVER_OK)

    UrllibHetznerClient("ctor-only", opener=opener).create_server(_server_req())
    assert captured["auth"] == "Bearer ctor-only"


# --- in-process broker (fake client) -----------------------------------------

def test_inprocess_broker_orders_calls():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake)
    result = broker.provision_box(
        server=_server_req(),
        volume=VolumeCreateRequest(name="vol-dep_a", size_gb=10, location="nbg1"),
        dns=DnsRecordRequest(zone_id="z1", name="dep_a.fleet.example", ipv4="", ttl=300),
    )

    # volume BEFORE server, DNS last.
    assert fake.calls == ["create_volume", "create_server", "upsert_dns_record"]
    # the server request carries the firewall id AND the created volume id (in-create, H-3).
    seen_server = fake.servers[0]
    assert seen_server.firewall_ids == (42,)
    assert "vol_1" in seen_server.volume_ids
    # DNS A record targeted the freshly-minted server IP (broker filled the empty ipv4).
    assert fake.dns[0].ipv4 == "203.0.113.1"

    assert isinstance(result, BrokerProvisionResult)
    assert result.server_id == "server_1"
    assert result.public_ipv4 == "203.0.113.1"
    assert result.volume_ids == ("vol_1",)
    assert result.dns_record_id == "dns_1"
    assert result.fqdn == "dep_a.fleet.example"


def test_broker_provision_skips_dns_when_no_provider():
    fake = FakeHetznerClient()
    result = InProcessHetznerBroker(fake).provision_box(
        server=_server_req(), volume=None, dns=None
    )
    assert fake.calls == ["create_server"]
    assert fake.dns == []
    assert result.fqdn == ""
    assert result.dns_record_id == ""
    assert result.volume_ids == ()


def test_broker_destroy_requires_confirm_and_is_unimplemented_in_p4():
    broker = InProcessHetznerBroker(FakeHetznerClient())
    with pytest.raises(ValueError):
        broker.destroy_box(server_id="server_1", volume_ids=(), dns_record_ids=(), confirm=False)
    with pytest.raises(NotImplementedError):
        broker.destroy_box(server_id="server_1", volume_ids=(), dns_record_ids=(), confirm=True)


# --- factory / A6 invariant --------------------------------------------------

def test_build_broker_rejects_remote_url_in_p4():
    settings = Settings(hetzner_broker_url="https://broker.internal", provisioner_backend="hetzner")
    with pytest.raises(RuntimeError, match="Phase 5"):
        build_hetzner_broker(settings, client=FakeHetznerClient())


def test_build_broker_forbids_live_inprocess_for_hetzner():
    # A6: production Hetzner in-process is forbidden without the dogfood flag.
    forbidden = Settings(
        provisioner_backend="hetzner", hetzner_broker_url="", hetzner_allow_inprocess_broker=False
    )
    with pytest.raises(RuntimeError, match="forbidden in production"):
        build_hetzner_broker(forbidden, client=FakeHetznerClient())

    # with the dogfood flag -> allowed, returns an in-process broker.
    allowed = Settings(
        provisioner_backend="hetzner", hetzner_broker_url="", hetzner_allow_inprocess_broker=True
    )
    broker = build_hetzner_broker(allowed, client=FakeHetznerClient())
    assert isinstance(broker, InProcessHetznerBroker)

    # dormant default (github) -> the guard never fires.
    dormant = Settings(provisioner_backend="github")
    assert isinstance(build_hetzner_broker(dormant, client=FakeHetznerClient()), InProcessHetznerBroker)


def test_fake_client_error_injection():
    fake = FakeHetznerClient(fail_on={"create_server"})
    with pytest.raises(HetznerApiError):
        fake.create_server(_server_req())
    # other methods still work
    assert fake.create_volume(VolumeCreateRequest(name="v", size_gb=10, location="nbg1")).volume_id == "vol_1"

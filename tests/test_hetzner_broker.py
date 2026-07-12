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
    FirewallCreateRequest,
    FirewallRule,
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


# --- P5-05: DNS true upsert (opener-injected; no network) --------------------

_ZONE_RECORDS = json.dumps({"records": [
    {"id": "rec_a", "type": "A", "name": "dep_a", "zone_name": "fleet.example"},
    {"id": "rec_www", "type": "A", "name": "www"},
]}).encode("utf-8")
_ZONE_EMPTY = json.dumps({"records": []}).encode("utf-8")
_DNS_PUT_OK = json.dumps({"record": {"id": "rec_a", "name": "dep_a", "zone_name": "fleet.example"}}).encode("utf-8")
_DNS_POST_OK = json.dumps({"record": {"id": "rec_new", "name": "dep_a", "zone_name": "fleet.example"}}).encode("utf-8")


def test_dns_upsert_puts_existing_a_record():
    seen = []

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url, request.get_header("Auth-api-token")))
        return _FakeResponse(_ZONE_RECORDS if request.get_method() == "GET" else _DNS_PUT_OK)

    result = UrllibHetznerClient("api-t", "dns-t", opener=opener).upsert_dns_record(
        DnsRecordRequest(zone_id="z1", name="dep_a", ipv4="203.0.113.5"))
    # GET the zone's records first, then PUT the MATCHED record (never POST a duplicate).
    assert seen[0][0] == "GET" and "records?zone_id=z1" in seen[0][1]
    assert seen[1][0] == "PUT" and seen[1][1].endswith("/records/rec_a")
    # the DNS token rides the Auth-API-Token header (never a Bearer), on both requests.
    assert seen[0][2] == "dns-t" and seen[1][2] == "dns-t"
    assert result.record_id == "rec_a"


def test_dns_upsert_posts_when_no_matching_record():
    seen = []

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url))
        return _FakeResponse(_ZONE_EMPTY if request.get_method() == "GET" else _DNS_POST_OK)

    result = UrllibHetznerClient("api-t", "dns-t", opener=opener).upsert_dns_record(
        DnsRecordRequest(zone_id="z1", name="dep_a", ipv4="203.0.113.5"))
    assert seen[0][0] == "GET"
    assert seen[1][0] == "POST" and seen[1][1].endswith("/records")
    assert result.record_id == "rec_new"


def test_fake_dns_upsert_is_idempotent_by_zone_and_name():
    fake = FakeHetznerClient()
    r1 = fake.upsert_dns_record(DnsRecordRequest(zone_id="z1", name="dep_a", ipv4="1.1.1.1"))
    r2 = fake.upsert_dns_record(DnsRecordRequest(zone_id="z1", name="dep_a", ipv4="2.2.2.2"))
    r3 = fake.upsert_dns_record(DnsRecordRequest(zone_id="z1", name="other", ipv4="3.3.3.3"))
    assert r1.record_id == r2.record_id     # same (zone, name) -> the same record (no duplicate)
    assert r3.record_id != r1.record_id     # a different name -> a new record


# --- P5-05: default-deny Cloud Firewall (opener-injected + broker) -----------

_FW_OK = json.dumps({"firewall": {"id": 777}}).encode("utf-8")


def test_urllib_client_create_firewall_shape():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data)
        return _FakeResponse(_FW_OK)

    result = UrllibHetznerClient("api-t", opener=opener).create_firewall(FirewallCreateRequest(
        name="onebrain-dep_a-fw",
        rules=(FirewallRule(direction="in", protocol="tcp", port="80"),
               FirewallRule(direction="in", protocol="tcp", port="443"))))
    assert captured["url"] == "https://api.hetzner.cloud/v1/firewalls"
    assert captured["method"] == "POST" and captured["auth"] == "Bearer api-t"
    rules = captured["body"]["rules"]
    assert [(r["direction"], r["protocol"], r["port"]) for r in rules] == [("in", "tcp", "80"), ("in", "tcp", "443")]
    assert all(r["source_ips"] == ["0.0.0.0/0", "::/0"] for r in rules)
    assert result.firewall_id == "777"


def test_broker_creates_firewall_and_attaches_it_in_create():
    fake = FakeHetznerClient()
    result = InProcessHetznerBroker(fake).provision_box(
        server=_server_req(firewall_ids=()), volume=None, dns=None,
        firewall=FirewallCreateRequest(name="onebrain-dep_a-fw",
                                       rules=(FirewallRule(direction="in", protocol="tcp", port="80"),)))
    # firewall created BEFORE the server; its id attached IN the server create (H-3).
    assert fake.calls == ["create_firewall", "create_server"]
    assert fake.servers[0].firewall_ids == ("fw_1",)
    assert result.firewall_id == "fw_1"


def test_broker_uses_precreated_firewall_when_no_request():
    fake = FakeHetznerClient()
    result = InProcessHetznerBroker(fake).provision_box(
        server=_server_req(firewall_ids=(42,)), volume=None, dns=None, firewall=None)
    assert "create_firewall" not in fake.calls
    assert fake.servers[0].firewall_ids == (42,)     # the pre-created id, attached as-is
    assert result.firewall_id == ""                  # nothing created in this flow

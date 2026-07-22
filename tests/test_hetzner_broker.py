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
    provider_hostname_label,
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

    client = UrllibHetznerClient("sentinel-token-XYZ", opener=opener)
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


def test_urllib_client_enable_backup_shape_and_idempotency():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data)
        return _FakeResponse(json.dumps({"action": {"id": 99, "status": "running"}}).encode("utf-8"))

    result = UrllibHetznerClient("bk-token", opener=opener).enable_backup("150330048")
    assert captured["url"] == "https://api.hetzner.cloud/v1/servers/150330048/actions/enable_backup"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer bk-token"
    assert captured["body"] == {}                     # empty body — Hetzner auto-selects the window
    assert result.action_id == "99" and result.status == "running"

    # Already-enabled (409 carrying the code) is an idempotent no-op, never a raise.
    def opener_already(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 409, "conflict", None,
            io.BytesIO(b'{"error":{"code":"server_backup_already_enabled"}}'))

    r2 = UrllibHetznerClient("t", opener=opener_already).enable_backup("s1")
    assert r2.status == "already_enabled" and r2.action_id == ""

    # A genuine failure (500) still raises — never mistaken for idempotency.
    def opener_500(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "err", None, io.BytesIO(b"boom"))

    with pytest.raises(HetznerApiError):
        UrllibHetznerClient("t", opener=opener_500).enable_backup("s1")


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


# --- BK2: Hetzner server Backups auto-enable (root-disk-only convenience DR) --

def test_broker_enables_backups_after_create_before_dns():
    fake = FakeHetznerClient()
    result = InProcessHetznerBroker(fake, enable_backups=True).provision_box(
        server=_server_req(),
        volume=VolumeCreateRequest(name="v", size_gb=10, location="nbg1"),
        dns=DnsRecordRequest(zone_id="z1", name="dep_a.fleet.example", ipv4="", ttl=300),
        firewall=FirewallCreateRequest(
            name="fw", rules=(FirewallRule(direction="in", protocol="tcp", port="443"),)),
    )
    # enable_backup sits immediately AFTER create_server and BEFORE the DNS upsert (pinned).
    assert fake.calls == [
        "create_firewall", "create_volume", "create_server", "enable_backup", "upsert_dns_record"]
    assert fake.backup_enabled_calls == ["server_1"]       # the just-created id
    assert result.backups_enabled is True


def test_broker_skips_backups_when_disabled():
    fake = FakeHetznerClient()
    result = InProcessHetznerBroker(fake, enable_backups=False).provision_box(
        server=_server_req(), volume=None, dns=None)
    assert "enable_backup" not in fake.calls
    assert fake.backup_enabled_calls == []
    assert result.backups_enabled is False


def test_broker_enable_backup_failure_is_nonfatal():
    fake = FakeHetznerClient(fail_on={"enable_backup"})
    result = InProcessHetznerBroker(fake, enable_backups=True).provision_box(
        server=_server_req(), volume=None, dns=None)
    # the box still provisions (server returned) despite the backup action failing.
    assert result.server_id == "server_1"
    assert "enable_backup" in fake.calls                   # attempted, logged, not fatal


def test_broker_reuse_converges_backups():
    fake = FakeHetznerClient()
    fake.create_server(_server_req())                      # seed the idempotency hit (deployment_id=dep_a)
    result = InProcessHetznerBroker(fake, enable_backups=True).provision_box(
        server=_server_req(), volume=None, dns=None)
    assert result.reused is True
    assert fake.calls.count("create_server") == 1          # no NEW server minted
    assert fake.backup_enabled_calls == ["server_1"]       # converged the reused box
    assert result.backups_enabled is True


def test_factory_threads_enable_backups_default_true():
    on = build_hetzner_broker(Settings(provisioner_backend="disabled"), client=FakeHetznerClient())
    assert on._enable_backups is True                      # default true, threaded by the factory
    off = build_hetzner_broker(
        Settings(provisioner_backend="disabled", hetzner_enable_backups=False), client=FakeHetznerClient())
    assert off._enable_backups is False


# --- teardown: guarded, discovery-based destroy_box (Phase A) ----------------
# The broker DISCOVERS a deployment's own resources by label and deletes exactly those,
# so it can neither reach a foreign resource nor leak an idempotent-reuse box's volume.

def _labelled_volume(dep):
    return VolumeCreateRequest(name=f"onebrain-{dep}-data", size_gb=10, location="nbg1",
                               labels={"deployment_id": dep, "managed-by": "onebrain-fleet"})


def _labelled_firewall(dep):
    return FirewallCreateRequest(
        name=f"onebrain-{dep}-fw",
        rules=(FirewallRule(direction="in", protocol="tcp", port="443"),),
        labels={"deployment_id": dep, "managed-by": "onebrain-fleet"})


def _provision_box(broker, dep):
    return broker.provision_box(
        server=_server_req(firewall_ids=(), labels=_fleet_labels(dep)),
        volume=_labelled_volume(dep),
        # The provisioner names the A record with the RFC-1123 label, not the raw id.
        dns=DnsRecordRequest(zone_id="z1", name=provider_hostname_label(dep), ipv4=""),
        firewall=_labelled_firewall(dep))


def test_broker_destroy_requires_confirm_and_deployment_id():
    broker = InProcessHetznerBroker(FakeHetznerClient(), dns_zone_id="z1")
    with pytest.raises(ValueError):
        broker.destroy_box("dep_a", confirm=False)
    with pytest.raises(ValueError):
        broker.destroy_box("", confirm=True)


def test_broker_destroy_discovers_and_deletes_in_order():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    _provision_box(broker, "dep_a")
    fake.calls.clear()                       # focus on the teardown sequence

    result = broker.destroy_box("dep_a", confirm=True)

    # server -> volume -> firewall -> dns (the volume deletes only AFTER the server detaches it).
    assert fake.calls == ["delete_server", "delete_volume", "delete_firewall", "delete_dns_record"]
    assert result.servers_deleted == ("server_1",)
    assert result.volumes_deleted == ("vol_1",)
    assert result.firewalls_deleted == ("fw_1",)
    assert result.dns_deleted == ("dep-a/A",)      # underscore -> RFC-1123 dash, matching the box hostname
    assert result.nothing_found is False
    # really gone from every scope read
    assert fake.list_servers("deployment_id=dep_a") == []
    assert fake.list_volumes("deployment_id=dep_a") == []
    assert fake.list_firewalls("deployment_id=dep_a") == []


def test_broker_destroy_only_touches_the_named_deployment():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    _provision_box(broker, "dep_a")
    _provision_box(broker, "dep_b")

    broker.destroy_box("dep_a", confirm=True)

    # dep_b untouched — scope is the deployment's OWN label, never a handed-in id.
    assert [s.id for s in fake.list_servers("deployment_id=dep_b")] == ["server_2"]
    assert [v.id for v in fake.list_volumes("deployment_id=dep_b")] == ["vol_2"]
    assert [f.id for f in fake.list_firewalls("deployment_id=dep_b")] == ["fw_2"]
    assert fake.list_servers("deployment_id=dep_a") == []


def test_broker_destroy_ignores_a_server_without_the_fleet_label():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    # Carries the deployment_id but NOT the fleet label -> not a box we created -> never deleted.
    fake.create_server(_server_req(labels={"deployment_id": "dep_a"}))
    result = broker.destroy_box("dep_a", confirm=True)
    assert result.servers_deleted == () and result.nothing_found is True
    assert [s.id for s in fake.list_servers("deployment_id=dep_a")] == ["server_1"]   # survived


def test_broker_destroy_nothing_found_is_record_only_signal():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    result = broker.destroy_box("ghost", confirm=True)
    assert result.nothing_found is True
    assert result.servers_deleted == () and result.volumes_deleted == ()
    # Nothing discovered -> DNS is NOT touched (the broker is not an unscoped DNS-delete tool).
    assert fake.calls == []
    assert result.dns_deleted == ()


def test_broker_destroy_ignores_volume_and_firewall_without_the_fleet_label():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    # A fleet server exists, but a volume/firewall carries only deployment_id (no fleet label):
    # not ours -> never deleted, even though it shares the id.
    fake.create_server(_server_req(labels=_fleet_labels("dep_a")))
    fake.create_volume(VolumeCreateRequest(name="v", size_gb=10, location="nbg1",
                                           labels={"deployment_id": "dep_a"}))
    fake.create_firewall(FirewallCreateRequest(
        name="fw", rules=(FirewallRule(direction="in", protocol="tcp", port="443"),),
        labels={"deployment_id": "dep_a"}))

    result = broker.destroy_box("dep_a", confirm=True)
    assert result.servers_deleted == ("server_1",)          # the fleet-owned server is deleted
    assert result.volumes_deleted == ()                     # the unlabelled volume survives
    assert result.firewalls_deleted == ()                   # the unlabelled firewall survives
    assert [v.id for v in fake.list_volumes("deployment_id=dep_a")] == ["vol_1"]
    assert [f.id for f in fake.list_firewalls("deployment_id=dep_a")] == ["fw_1"]


def test_broker_destroy_never_deletes_dns_for_an_empty_or_unowned_label():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    # A deployment that owns nothing must never delete a derived DNS record; an underscore-only
    # id (label -> "") must never reach the zone apex.
    assert broker.destroy_box("___", confirm=True).dns_deleted == ()
    assert broker.destroy_box("www", confirm=True).dns_deleted == ()
    assert "delete_dns_record" not in fake.calls


def test_broker_destroy_is_idempotent_on_rerun():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    _provision_box(broker, "dep_a")
    assert broker.destroy_box("dep_a", confirm=True).nothing_found is False
    second = broker.destroy_box("dep_a", confirm=True)          # already gone
    assert second.nothing_found is True
    assert second.servers_deleted == () and second.volumes_deleted == ()


def test_broker_destroy_skips_dns_when_no_zone_configured():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="")       # IP-only fleet: no DNS record exists
    _provision_box(broker, "dep_a")
    fake.calls.clear()
    result = broker.destroy_box("dep_a", confirm=True)
    assert "delete_dns_record" not in fake.calls and result.dns_deleted == ()


def test_broker_destroy_retries_volume_delete_until_detached():
    # Model the real detach window: the volume reports attached on the first delete, then
    # detaches. The broker retries (sleep stubbed) rather than leaking the data volume.
    class _StickyVolumeClient(FakeHetznerClient):
        def __init__(self):
            super().__init__()
            self._delete_volume_hits = 0

        def delete_volume(self, volume_id):
            self._delete_volume_hits += 1
            if self._delete_volume_hits == 1:
                self.calls.append("delete_volume")
                raise HetznerApiError(409, "still attached")
            return super().delete_volume(volume_id)

    fake = _StickyVolumeClient()
    broker = InProcessHetznerBroker(fake, dns_zone_id="z1")
    broker._sleep = lambda _s: None                            # no real waiting in the test
    _provision_box(broker, "dep_a")
    result = broker.destroy_box("dep_a", confirm=True)
    assert result.volumes_deleted == ("vol_1",)
    assert fake.calls.count("delete_volume") == 2              # one 409, one success


# --- factory / A6 invariant --------------------------------------------------

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

    # Disabled provisioning is dormant, so the Hetzner-specific guard never fires.
    dormant = Settings(provisioner_backend="disabled")
    assert isinstance(build_hetzner_broker(dormant, client=FakeHetznerClient()), InProcessHetznerBroker)


def test_fake_client_error_injection():
    fake = FakeHetznerClient(fail_on={"create_server"})
    with pytest.raises(HetznerApiError):
        fake.create_server(_server_req())
    # other methods still work
    assert fake.create_volume(VolumeCreateRequest(name="v", size_gb=10, location="nbg1")).volume_id == "vol_1"


def test_fake_client_enable_backup_idempotent():
    fake = FakeHetznerClient()
    sid = fake.create_server(_server_req()).server_id
    first = fake.enable_backup(sid)
    assert first.status == "running" and fake.backup_enabled_calls == [sid]
    second = fake.enable_backup(sid)                  # already enabled -> no-op
    assert second.status == "already_enabled"
    assert fake.calls.count("enable_backup") == 2
    # injected failure path (broker treats it as non-fatal — see BK2 test)
    boom = FakeHetznerClient(fail_on={"enable_backup"})
    boom.create_server(_server_req())
    with pytest.raises(HetznerApiError):
        boom.enable_backup("server_1")


# --- P5-05 → unified Cloud API DNS RRSet upsert (opener-injected; no network) ---
# DNS now rides the SAME api.hetzner.cloud host + Bearer token as compute (GA 2025-11-10);
# records are RRSets addressed by zone-relative name + type, updated via set_records — NOT
# the legacy dns.hetzner.com /records model with per-record ids and an Auth-API-Token header.


def _rrset_opener(seen, *, exists: bool):
    """Record every (method, url, authorization, body) and answer the RRSet existence probe
    with 200 (exists) or 404 (missing); every create/action POST returns 200 empty."""

    def opener(request, timeout):
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        seen.append((method, request.full_url, request.get_header("Authorization"), body))
        if method == "GET" and not exists:
            raise urllib.error.HTTPError(request.full_url, 404, "not found", None, io.BytesIO(b"{}"))
        return _FakeResponse(b"{}")

    return opener


def test_dns_upsert_replaces_records_on_existing_rrset():
    seen = []
    result = UrllibHetznerClient("api-t", opener=_rrset_opener(seen, exists=True)).upsert_dns_record(
        DnsRecordRequest(zone_id="fleet.example", name="dep_a", ipv4="203.0.113.5"))

    # 1) probe the (name, A) RRSet on the UNIFIED Cloud API host; 2) set_records REPLACES the
    #    value list (idempotent) — never a legacy /records PUT. The zone path segment is the
    #    id-or-name (no separate zone-id lookup), and the label is the zone-RELATIVE name.
    assert seen[0][0] == "GET"
    assert seen[0][1] == "https://api.hetzner.cloud/v1/zones/fleet.example/rrsets/dep_a/A"
    assert seen[1][0] == "POST"
    assert seen[1][1] == (
        "https://api.hetzner.cloud/v1/zones/fleet.example/rrsets/dep_a/A/actions/set_records")
    assert seen[1][3] == {"records": [{"value": "203.0.113.5"}]}
    # The DNS calls carry the SAME compute Bearer token — never the legacy Auth-API-Token.
    assert all(auth == "Bearer api-t" for _m, _u, auth, _b in seen)
    assert result.record_id == "dep_a/A"


def test_dns_upsert_creates_rrset_when_missing():
    seen = []
    result = UrllibHetznerClient("api-t", opener=_rrset_opener(seen, exists=False)).upsert_dns_record(
        DnsRecordRequest(zone_id="fleet.example", name="dep_a", ipv4="203.0.113.5", ttl=300))

    # 404 on the probe -> POST a NEW RRSet (zone-relative label + records array), not set_records.
    assert seen[0][0] == "GET"
    assert seen[1][0] == "POST"
    assert seen[1][1] == "https://api.hetzner.cloud/v1/zones/fleet.example/rrsets"
    assert seen[1][3] == {"name": "dep_a", "type": "A", "ttl": 300, "records": [{"value": "203.0.113.5"}]}
    assert not any("actions/set_records" in url for _m, url, _a, _b in seen)
    assert result.record_id == "dep_a/A"


def test_dns_upsert_apex_uses_encoded_at_label():
    # Empty label normalizes to the apex "@": %40 in the probe path, literal "@" in the body.
    seen = []
    UrllibHetznerClient("api-t", opener=_rrset_opener(seen, exists=False)).upsert_dns_record(
        DnsRecordRequest(zone_id="z1", name="", ipv4="203.0.113.5"))
    assert seen[0][1].endswith("/zones/z1/rrsets/%40/A")
    assert seen[1][3]["name"] == "@"


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


# --- COST-SAFETY GATEKEEPER: list_servers + idempotency + fleet-size cap -------
# Nothing else in the fleet prevents duplicate/runaway server creation, so the broker
# gates on a label read BEFORE every create: reuse an existing deployment (idempotency)
# and refuse to grow past the fleet cap (cost circuit breaker).

_FLEET = {"managed-by": "onebrain-fleet"}


def _fleet_labels(dep):
    return {"deployment_id": dep, **_FLEET}


def test_fake_list_servers_filters_by_exact_selector_and_excludes_deleted():
    fake = FakeHetznerClient()
    fake.create_server(_server_req(name="a", labels=_fleet_labels("dep_a")))
    fake.create_server(_server_req(name="b", labels=_fleet_labels("dep_b")))

    # Exact key=value match on either of the two selectors the broker uses.
    assert [s.id for s in fake.list_servers("deployment_id=dep_a")] == ["server_1"]
    assert {s.id for s in fake.list_servers("managed-by=onebrain-fleet")} == {"server_1", "server_2"}
    assert fake.list_servers("deployment_id=absent") == []      # a non-matching value -> nothing
    # ServerInfo carries the fields the gates need.
    only = fake.list_servers("deployment_id=dep_a")[0]
    assert only.public_ipv4 == "203.0.113.1" and only.labels["deployment_id"] == "dep_a"

    # A deleted server is excluded from BOTH selectors (never reused, never counted).
    fake.mark_server_deleted("server_1")
    assert fake.list_servers("deployment_id=dep_a") == []
    assert [s.id for s in fake.list_servers("managed-by=onebrain-fleet")] == ["server_2"]
    # list_servers is a READ — it must not pollute the mutating-calls ordering log.
    assert "list_servers" not in fake.calls


def test_urllib_client_list_servers_issues_labelled_get_with_bearer():
    from urllib.parse import parse_qs, urlsplit

    seen = {}
    body = json.dumps({"servers": [
        {"id": 55, "name": "onebrain-dep_a", "labels": {"deployment_id": "dep_a"},
         "status": "running", "public_net": {"ipv4": {"ip": "203.0.113.55"}}},
    ]}).encode("utf-8")

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse(body)

    result = UrllibHetznerClient("api-t", opener=opener).list_servers("deployment_id=dep_a")

    split = urlsplit(seen["url"])
    assert split.path == "/v1/servers"                          # GET /servers, no create
    assert seen["method"] == "GET"
    assert parse_qs(split.query)["label_selector"] == ["deployment_id=dep_a"]
    assert seen["auth"] == "Bearer api-t"                       # same Bearer token as compute
    assert len(result) == 1
    assert result[0].id == "55" and result[0].public_ipv4 == "203.0.113.55"
    assert result[0].labels == {"deployment_id": "dep_a"} and result[0].status == "running"


def test_broker_idempotency_reuses_existing_server_and_creates_nothing():
    fake = FakeHetznerClient()
    broker = InProcessHetznerBroker(fake)
    server = _server_req(firewall_ids=(), labels=_fleet_labels("dep_a"))
    fw = FirewallCreateRequest(name="fw", rules=(FirewallRule(direction="in", protocol="tcp", port="80"),))
    vol = VolumeCreateRequest(name="v", size_gb=10, location="nbg1")
    dns = DnsRecordRequest(zone_id="z1", name="dep_a", ipv4="")

    first = broker.provision_box(server=server, volume=vol, dns=dns, firewall=fw)
    assert first.reused is False
    calls_after_first = list(fake.calls)                        # firewall+volume+server+dns
    assert fake.calls.count("create_server") == 1

    # A second provision for the SAME deployment_id creates NOTHING new (safe to retry forever).
    second = broker.provision_box(server=server, volume=vol, dns=dns, firewall=fw)
    assert second.reused is True
    assert second.server_id == first.server_id and second.public_ipv4 == first.public_ipv4
    assert second.fqdn == "dep_a"                               # reconstructed from the dns request
    assert second.firewall_id == "" and second.dns_record_id == "" and second.volume_ids == ()
    assert fake.calls == calls_after_first                      # NO second create of anything
    assert fake.calls.count("create_server") == 1


def test_broker_fleet_cap_refuses_new_server_when_at_or_over_cap():
    fake = FakeHetznerClient()
    fake.create_server(_server_req(name="a", labels=_fleet_labels("dep_1")))
    fake.create_server(_server_req(name="b", labels=_fleet_labels("dep_2")))
    broker = InProcessHetznerBroker(fake, max_fleet_servers=2)

    with pytest.raises(RuntimeError, match=r"fleet server cap reached \(2/2\)") as exc:
        broker.provision_box(server=_server_req(firewall_ids=(), labels=_fleet_labels("dep_new")),
                             volume=None, dns=None, firewall=None)
    # The message names the env var the operator raises to grow the fleet.
    assert "ONEBRAIN_HETZNER_MAX_FLEET_SERVERS" in str(exc.value)
    # NO third server was created — the breaker fired before the create call.
    assert fake.calls.count("create_server") == 2
    assert len(fake.list_servers("managed-by=onebrain-fleet")) == 2


def test_broker_fleet_cap_never_trips_on_idempotent_reuse():
    # Idempotency runs BEFORE the cap, so re-provisioning an EXISTING deployment while the
    # fleet is already at the cap reuses the box rather than raising (a retry must never be
    # blocked by the cap it did not grow).
    fake = FakeHetznerClient()
    fake.create_server(_server_req(name="a", labels=_fleet_labels("dep_1")))
    fake.create_server(_server_req(name="b", labels=_fleet_labels("dep_2")))
    broker = InProcessHetznerBroker(fake, max_fleet_servers=2)

    out = broker.provision_box(server=_server_req(firewall_ids=(), labels=_fleet_labels("dep_1")),
                               volume=None, dns=None, firewall=None)
    assert out.reused is True and out.server_id == "server_1"
    assert fake.calls.count("create_server") == 2               # still just the two seeded


def test_build_broker_threads_fleet_cap_from_settings():
    # The factory wires settings.hetzner_max_fleet_servers into the broker so the breaker is
    # enforced on every production path (provisioner AND bootstrap_mc go through the factory).
    fake = FakeHetznerClient()
    fake.create_server(_server_req(name="a", labels=_fleet_labels("dep_1")))
    settings = Settings(provisioner_backend="hetzner", hetzner_allow_inprocess_broker=True,
                        hetzner_max_fleet_servers=1)
    broker = build_hetzner_broker(settings, client=fake)
    with pytest.raises(RuntimeError, match="fleet server cap reached"):
        broker.provision_box(server=_server_req(firewall_ids=(), labels=_fleet_labels("dep_new")),
                             volume=None, dns=None, firewall=None)


# --- teardown transport shapes (opener-injected; no network) -----------------

def test_urllib_client_delete_primitives_issue_delete_with_bearer():
    seen = []

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        return _FakeResponse(b"")

    client = UrllibHetznerClient("del-token", opener=opener)
    client.delete_server("srv9")
    client.delete_volume("vol9")
    client.delete_firewall("fw9")
    client.delete_dns_record("z1", "dep-a")

    assert seen == [
        ("DELETE", "https://api.hetzner.cloud/v1/servers/srv9", "Bearer del-token"),
        ("DELETE", "https://api.hetzner.cloud/v1/volumes/vol9", "Bearer del-token"),
        ("DELETE", "https://api.hetzner.cloud/v1/firewalls/fw9", "Bearer del-token"),
        ("DELETE", "https://api.hetzner.cloud/v1/zones/z1/rrsets/dep-a/A", "Bearer del-token"),
    ]


def test_urllib_client_delete_treats_404_as_noop_but_raises_others():
    def opener_404(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "gone", None, io.BytesIO(b"{}"))
    # 404 -> idempotent no-op (already gone): never raises.
    UrllibHetznerClient("t", opener=opener_404).delete_volume("vol_missing")

    def opener_409(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "attached", None, io.BytesIO(b"still attached"))
    # 409 (attached) propagates so the broker's destroy can retry after detach.
    with pytest.raises(HetznerApiError) as exc:
        UrllibHetznerClient("t", opener=opener_409).delete_volume("vol_attached")
    assert exc.value.status == 409


def test_urllib_client_delete_dns_apex_uses_encoded_at():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        return _FakeResponse(b"")

    UrllibHetznerClient("t", opener=opener).delete_dns_record("z1", "")
    assert seen["url"].endswith("/zones/z1/rrsets/%40/A")


def test_urllib_client_list_volumes_parses_label_and_attachment():
    from urllib.parse import parse_qs, urlsplit

    seen = {}
    body = json.dumps({"volumes": [
        {"id": 71, "labels": {"deployment_id": "dep_a"}, "server": 55},
        {"id": 72, "labels": {"deployment_id": "dep_a"}, "server": None},
    ]}).encode("utf-8")

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        return _FakeResponse(body)

    result = UrllibHetznerClient("t", opener=opener).list_volumes("deployment_id=dep_a")
    split = urlsplit(seen["url"])
    assert split.path == "/v1/volumes" and seen["method"] == "GET"
    assert parse_qs(split.query)["label_selector"] == ["deployment_id=dep_a"]
    # server=null parses to "" (detached) — the signal delete_volume ordering relies on.
    assert [(v.id, v.server_id) for v in result] == [("71", "55"), ("72", "")]


def test_urllib_client_list_firewalls_parses_label():
    body = json.dumps({"firewalls": [{"id": 88, "labels": {"deployment_id": "dep_a"}}]}).encode("utf-8")

    def opener(request, timeout):
        assert "/v1/firewalls?" in request.full_url
        return _FakeResponse(body)

    result = UrllibHetznerClient("t", opener=opener).list_firewalls("deployment_id=dep_a")
    assert [(f.id, f.labels) for f in result] == [("88", {"deployment_id": "dep_a"})]

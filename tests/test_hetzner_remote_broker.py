"""Remote MC-to-broker transport and broker-host boundary tests."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.provisioning.hetzner.broker import build_hetzner_broker
from app.provisioning.hetzner.broker_service import BrokerSettings, create_broker_app
from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    FirewallCreateRequest,
    FirewallRule,
    ServerCreateRequest,
    VolumeCreateRequest,
)
from app.provisioning.hetzner.fake import FakeHetznerClient
from app.provisioning.hetzner.remote import (
    RemoteHetznerBroker,
    encode_provision_request,
)
from app.servicekeys.base import hash_secret


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self, _limit=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _server(*, location="nbg1", labels=None, user_data="#cloud-config\n"):
    return ServerCreateRequest(
        name="onebrain-dep-a",
        server_type="cx23",
        image="ubuntu-24.04",
        location=location,
        user_data=user_data,
        labels=labels or {"deployment_id": "dep_a", "managed-by": "onebrain-fleet"},
    )


def _request_payload(*, location="nbg1", firewall_port="443"):
    return encode_provision_request(
        server=_server(location=location),
        volume=VolumeCreateRequest(
            name="onebrain-dep-a-data", size_gb=10, location=location, labels={"deployment_id": "dep_a"}
        ),
        dns=DnsRecordRequest(zone_id="fleet.example", name="dep-a", ipv4="", ttl=300),
        firewall=FirewallCreateRequest(
            name="onebrain-dep-a-fw",
            rules=(
                FirewallRule(direction="in", protocol="tcp", port="80"),
                FirewallRule(direction="in", protocol="tcp", port=firewall_port),
            ),
            labels={"deployment_id": "dep_a"},
        ),
    )


def _broker_settings(**overrides):
    values = {
        "api_token": "hcloud-secret-not-for-mc",
        "mc_token_hash": hash_secret("mc-broker-credential"),
        "dns_zone_id": "fleet.example",
        "max_fleet_servers": 3,
    }
    values.update(overrides)
    return BrokerSettings(**values)


def test_remote_broker_serializes_typed_request_and_sanitized_response():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["authorization"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        return _Response({
            "server_id": "123",
            "public_ipv4": "203.0.113.10",
            "volume_ids": ["456"],
            "dns_record_id": "dep-a/A",
            "fqdn": "dep-a.fleet.example",
            "firewall_id": "789",
            "reused": False,
            "backups_enabled": True,
        })

    broker = RemoteHetznerBroker(
        "https://broker.onlyonebrain.internal",
        "mc-broker-credential",
        client_certificate_file="/run/mc-client.crt",
        client_key_file="/run/mc-client.key",
        timeout_seconds=7,
        opener=opener,
    )
    result = broker.provision_box(server=_server(), volume=None, dns=None, firewall=None)

    assert seen["url"] == "https://broker.onlyonebrain.internal/v1/provision"
    assert seen["timeout"] == 7
    assert seen["authorization"] == "Bearer mc-broker-credential"
    assert seen["body"]["server"]["labels"]["managed-by"] == "onebrain-fleet"
    assert result.server_id == "123" and result.volume_ids == ("456",)


def test_remote_broker_rejects_malformed_response_without_echoing_request():
    broker = RemoteHetznerBroker(
        "https://broker.internal",
        "mc-broker-credential",
        client_certificate_file="client.crt",
        client_key_file="client.key",
        opener=lambda _request, _timeout: _Response({"unexpected": "response"}),
    )
    with pytest.raises(RuntimeError, match="invalid response"):
        broker.provision_box(server=_server(user_data="opaque bootstrap material"))


def test_remote_factory_requires_mtls_material_and_forbids_mc_hcloud_token():
    settings = Settings(
        provisioner_backend="hetzner",
        hetzner_broker_url="https://broker.internal",
        hetzner_broker_credential="credential",
        hetzner_broker_client_certificate_file="client.crt",
        hetzner_broker_client_key_file="client.key",
    )
    assert isinstance(build_hetzner_broker(settings), RemoteHetznerBroker)

    with pytest.raises(RuntimeError, match="must not hold"):
        build_hetzner_broker(Settings(
            provisioner_backend="hetzner",
            hetzner_broker_url="https://broker.internal",
            hetzner_broker_credential="credential",
            hetzner_broker_client_certificate_file="client.crt",
            hetzner_broker_client_key_file="client.key",
            hetzner_api_token="forbidden-on-mc",
        ))

    with pytest.raises(ValueError, match="certificate and key"):
        build_hetzner_broker(Settings(
            provisioner_backend="hetzner",
            hetzner_broker_url="https://broker.internal",
            hetzner_broker_credential="credential",
        ))


def test_broker_host_rejects_unauthenticated_requests_before_cloud_calls():
    fake = FakeHetznerClient()
    app = create_broker_app(settings=_broker_settings(), client=fake)
    response = TestClient(app).post("/v1/provision", json=_request_payload())

    assert response.status_code == 401
    assert fake.calls == []
    assert TestClient(app).get("/docs").status_code == 404


def test_broker_host_rejects_disallowed_inputs_before_cloud_calls():
    fake = FakeHetznerClient()
    app = create_broker_app(settings=_broker_settings(), client=fake)
    response = TestClient(app).post(
        "/v1/provision",
        json=_request_payload(location="ash-datacenter"),
        headers={"Authorization": "Bearer mc-broker-credential"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid provision request"}
    assert fake.calls == []


def test_broker_host_rejects_non_default_deny_firewall_before_cloud_calls():
    fake = FakeHetznerClient()
    app = create_broker_app(settings=_broker_settings(), client=fake)
    response = TestClient(app).post(
        "/v1/provision",
        json=_request_payload(firewall_port="5432"),
        headers={"Authorization": "Bearer mc-broker-credential"},
    )

    assert response.status_code == 400
    assert fake.calls == []


def test_broker_host_provisions_valid_request_without_exposing_credentials():
    fake = FakeHetznerClient()
    app = create_broker_app(settings=_broker_settings(), client=fake)
    response = TestClient(app).post(
        "/v1/provision",
        json=_request_payload(),
        headers={"Authorization": "Bearer mc-broker-credential"},
    )

    assert response.status_code == 200
    assert response.json()["server_id"] == "server_1"
    assert fake.calls == ["create_firewall", "create_volume", "create_server", "enable_backup", "upsert_dns_record"]
    rendered = response.text
    assert "hcloud-secret-not-for-mc" not in rendered
    assert "mc-broker-credential" not in rendered

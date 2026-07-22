"""Typed HTTPS transport for the isolated Hetzner broker.

Mission Control uses this module to request one bounded provisioning operation.
It never reads a Hetzner token. The broker re-validates every decoded field, so
this transport deliberately carries only the existing typed request shapes.
"""

from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.provisioning.hetzner.broker import BrokerDestroyResult, BrokerProvisionResult
from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    FirewallCreateRequest,
    FirewallRule,
    ServerCreateRequest,
    VolumeCreateRequest,
)

_MAX_RESPONSE_BYTES = 64 * 1024


def _strict_dict(value: Any, *, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid broker payload")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid broker payload")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("invalid broker payload")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("invalid broker payload")
    return value


def _labels(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError("invalid broker payload")
    return dict(value)


def _identifier_list(value: Any) -> tuple[Any, ...]:
    items = _list(value)
    if not all(isinstance(item, (str, int)) and not isinstance(item, bool) for item in items):
        raise ValueError("invalid broker payload")
    return tuple(items)


def encode_provision_request(
    *,
    server: ServerCreateRequest,
    volume: VolumeCreateRequest | None,
    dns: DnsRecordRequest | None,
    firewall: FirewallCreateRequest | None,
) -> dict[str, Any]:
    """Serialize the existing frozen request types without exposing provider tokens."""

    return {
        "server": {
            "name": server.name,
            "server_type": server.server_type,
            "image": server.image,
            "location": server.location,
            "user_data": server.user_data,
            "ssh_key_ids": list(server.ssh_key_ids),
            "firewall_ids": list(server.firewall_ids),
            "volume_ids": list(server.volume_ids),
            "labels": dict(server.labels),
        },
        "volume": None if volume is None else {
            "name": volume.name,
            "size_gb": volume.size_gb,
            "location": volume.location,
            "labels": dict(volume.labels),
        },
        "dns": None if dns is None else {
            "zone_id": dns.zone_id,
            "name": dns.name,
            "ipv4": dns.ipv4,
            "ttl": dns.ttl,
        },
        "firewall": None if firewall is None else {
            "name": firewall.name,
            "rules": [
                {
                    "direction": rule.direction,
                    "protocol": rule.protocol,
                    "port": rule.port,
                    "source_ips": list(rule.source_ips),
                }
                for rule in firewall.rules
            ],
            "labels": dict(firewall.labels),
        },
    }


def decode_provision_request(value: Any) -> tuple[
    ServerCreateRequest,
    VolumeCreateRequest | None,
    DnsRecordRequest | None,
    FirewallCreateRequest | None,
]:
    """Decode a complete, exact broker request. Unknown fields fail closed."""

    body = _strict_dict(value, keys={"server", "volume", "dns", "firewall"})
    server_data = _strict_dict(
        body["server"],
        keys={"name", "server_type", "image", "location", "user_data", "ssh_key_ids", "firewall_ids", "volume_ids", "labels"},
    )
    server = ServerCreateRequest(
        name=_string(server_data["name"]),
        server_type=_string(server_data["server_type"]),
        image=_string(server_data["image"]),
        location=_string(server_data["location"]),
        user_data=_string(server_data["user_data"]),
        ssh_key_ids=_identifier_list(server_data["ssh_key_ids"]),
        firewall_ids=_identifier_list(server_data["firewall_ids"]),
        volume_ids=_identifier_list(server_data["volume_ids"]),
        labels=_labels(server_data["labels"]),
    )

    volume = None
    if body["volume"] is not None:
        volume_data = _strict_dict(body["volume"], keys={"name", "size_gb", "location", "labels"})
        size_gb = volume_data["size_gb"]
        if not isinstance(size_gb, int) or isinstance(size_gb, bool):
            raise ValueError("invalid broker payload")
        volume = VolumeCreateRequest(
            name=_string(volume_data["name"]),
            size_gb=size_gb,
            location=_string(volume_data["location"]),
            labels=_labels(volume_data["labels"]),
        )

    dns = None
    if body["dns"] is not None:
        dns_data = _strict_dict(body["dns"], keys={"zone_id", "name", "ipv4", "ttl"})
        ttl = dns_data["ttl"]
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            raise ValueError("invalid broker payload")
        dns = DnsRecordRequest(
            zone_id=_string(dns_data["zone_id"]),
            name=_string(dns_data["name"]),
            ipv4=_string(dns_data["ipv4"]),
            ttl=ttl,
        )

    firewall = None
    if body["firewall"] is not None:
        firewall_data = _strict_dict(body["firewall"], keys={"name", "rules", "labels"})
        rules: list[FirewallRule] = []
        for raw_rule in _list(firewall_data["rules"]):
            rule_data = _strict_dict(raw_rule, keys={"direction", "protocol", "port", "source_ips"})
            source_ips = _list(rule_data["source_ips"])
            if not all(isinstance(item, str) for item in source_ips):
                raise ValueError("invalid broker payload")
            rules.append(FirewallRule(
                direction=_string(rule_data["direction"]),
                protocol=_string(rule_data["protocol"]),
                port=_string(rule_data["port"]),
                source_ips=tuple(source_ips),
            ))
        firewall = FirewallCreateRequest(
            name=_string(firewall_data["name"]), rules=tuple(rules), labels=_labels(firewall_data["labels"])
        )
    return server, volume, dns, firewall


def encode_provision_result(result: BrokerProvisionResult) -> dict[str, Any]:
    return {
        "server_id": result.server_id,
        "public_ipv4": result.public_ipv4,
        "volume_ids": list(result.volume_ids),
        "dns_record_id": result.dns_record_id,
        "fqdn": result.fqdn,
        "firewall_id": result.firewall_id,
        "reused": result.reused,
        "backups_enabled": result.backups_enabled,
    }


def decode_provision_result(value: Any) -> BrokerProvisionResult:
    body = _strict_dict(
        value,
        keys={"server_id", "public_ipv4", "volume_ids", "dns_record_id", "fqdn", "firewall_id", "reused", "backups_enabled"},
    )
    volume_ids = _list(body["volume_ids"])
    if not all(isinstance(item, str) for item in volume_ids):
        raise ValueError("invalid broker response")
    return BrokerProvisionResult(
        server_id=_string(body["server_id"]),
        public_ipv4=_string(body["public_ipv4"]),
        volume_ids=tuple(volume_ids),
        dns_record_id=_string(body["dns_record_id"]),
        fqdn=_string(body["fqdn"]),
        firewall_id=_string(body["firewall_id"]),
        reused=_bool(body["reused"]),
        backups_enabled=_bool(body["backups_enabled"]),
    )


def encode_destroy_request(*, deployment_id: str) -> dict[str, Any]:
    """Serialize a teardown request. Deliberately JUST the deployment id: the broker
    DISCOVERS the resources to delete by that label — MC never hands it raw resource ids,
    so a bad manifest can't point the broker at a foreign volume/firewall/DNS record."""
    return {"deployment_id": deployment_id}


def decode_destroy_request(value: Any) -> str:
    """Decode a complete, exact teardown request. Unknown fields fail closed."""
    body = _strict_dict(value, keys={"deployment_id"})
    return _string(body["deployment_id"])


def encode_destroy_result(result: BrokerDestroyResult) -> dict[str, Any]:
    return {
        "deployment_id": result.deployment_id,
        "servers_deleted": list(result.servers_deleted),
        "volumes_deleted": list(result.volumes_deleted),
        "firewalls_deleted": list(result.firewalls_deleted),
        "dns_deleted": list(result.dns_deleted),
        "nothing_found": result.nothing_found,
    }


def _string_tuple(value: Any) -> tuple[str, ...]:
    items = _list(value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError("invalid broker response")
    return tuple(items)


def decode_destroy_result(value: Any) -> BrokerDestroyResult:
    body = _strict_dict(
        value,
        keys={"deployment_id", "servers_deleted", "volumes_deleted", "firewalls_deleted",
              "dns_deleted", "nothing_found"},
    )
    return BrokerDestroyResult(
        deployment_id=_string(body["deployment_id"]),
        servers_deleted=_string_tuple(body["servers_deleted"]),
        volumes_deleted=_string_tuple(body["volumes_deleted"]),
        firewalls_deleted=_string_tuple(body["firewalls_deleted"]),
        dns_deleted=_string_tuple(body["dns_deleted"]),
        nothing_found=_bool(body["nothing_found"]),
    )


class RemoteHetznerBroker:
    """Mission Control client for a dedicated mTLS-protected broker host."""

    def __init__(
        self,
        url: str,
        credential: str,
        *,
        client_certificate_file: str,
        client_key_file: str,
        ca_file: str = "",
        timeout_seconds: float = 10.0,
        opener=None,
    ):
        parsed = urlsplit((url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("hetzner broker URL must be an HTTPS origin without query or fragment")
        if not credential:
            raise ValueError("hetzner broker credential is required")
        if not client_certificate_file or not client_key_file:
            raise ValueError("hetzner broker client certificate and key are required")
        if timeout_seconds <= 0:
            raise ValueError("hetzner broker timeout must be positive")
        self._url = url.rstrip("/")
        self._credential = credential
        self._client_certificate_file = client_certificate_file
        self._client_key_file = client_key_file
        self._ca_file = ca_file
        self._timeout_seconds = float(timeout_seconds)
        self._opener = opener

    def _ssl_context(self) -> ssl.SSLContext:
        try:
            context = ssl.create_default_context(cafile=self._ca_file or None)
            context.load_cert_chain(self._client_certificate_file, self._client_key_file)
            return context
        except Exception as exc:
            raise RuntimeError("unable to load Hetzner broker TLS client material") from exc

    def _open(self, request: Request):
        if self._opener is not None:
            return self._opener(request, self._timeout_seconds)
        return urlopen(request, timeout=self._timeout_seconds, context=self._ssl_context())

    def provision_box(
        self,
        *,
        server: ServerCreateRequest,
        volume: VolumeCreateRequest | None = None,
        dns: DnsRecordRequest | None = None,
        firewall: FirewallCreateRequest | None = None,
    ) -> BrokerProvisionResult:
        payload = json.dumps(
            encode_provision_request(server=server, volume=volume, dns=dns, firewall=firewall),
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self._url}/v1/provision",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._credential}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise RuntimeError(f"remote Hetzner broker rejected provisioning (HTTP {exc.code})") from exc
        except URLError as exc:
            raise RuntimeError("remote Hetzner broker is unavailable") from exc
        except OSError as exc:
            raise RuntimeError("remote Hetzner broker transport failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("remote Hetzner broker returned an oversized response")
        try:
            return decode_provision_result(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("remote Hetzner broker returned an invalid response") from exc

    def destroy_box(self, deployment_id: str, *, confirm: bool) -> BrokerDestroyResult:
        if not confirm:
            raise ValueError("destroy requires explicit confirm=True")
        payload = json.dumps(
            encode_destroy_request(deployment_id=deployment_id), separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self._url}/v1/destroy",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._credential}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise RuntimeError(f"remote Hetzner broker rejected teardown (HTTP {exc.code})") from exc
        except URLError as exc:
            raise RuntimeError("remote Hetzner broker is unavailable") from exc
        except OSError as exc:
            raise RuntimeError("remote Hetzner broker transport failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("remote Hetzner broker returned an oversized response")
        try:
            return decode_destroy_result(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("remote Hetzner broker returned an invalid response") from exc

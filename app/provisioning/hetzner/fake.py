"""In-memory `HetznerClient` for tests (P4-01). Deterministic ids
(server_1, vol_1, dns_1, ...), records every request for assertions, and supports
per-method error injection so P4-03 can exercise the `dispatch_failed` path. NO
network, ever — this is the object every Phase-4 provisioner/broker test runs
against."""

from __future__ import annotations

from typing import List

from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    DnsRecordResult,
    FirewallCreateRequest,
    FirewallCreateResult,
    HetznerApiError,
    ServerCreateRequest,
    ServerCreateResult,
    VolumeCreateRequest,
    VolumeCreateResult,
)


class FakeHetznerClient:
    """In-memory Hetzner. Deterministic ids. `.servers`/`.volumes`/`.dns`/`.firewalls`
    hold the *Request objects seen; `.calls` is the ordered method-name log (ordering
    assertions). `fail_on={"create_server"}` raises HetznerApiError for that method."""

    def __init__(self, *, fail_on=frozenset()):
        self.fail_on = set(fail_on)
        self.servers: List[ServerCreateRequest] = []
        self.volumes: List[VolumeCreateRequest] = []
        self.dns: List[DnsRecordRequest] = []
        self.firewalls: List[FirewallCreateRequest] = []
        self.calls: List[str] = []
        self._server_n = 0
        self._volume_n = 0
        self._dns_n = 0
        self._firewall_n = 0
        # (zone_id, name) -> record_id, so a second upsert of the same A record returns
        # the SAME id (models the true upsert without a network round-trip).
        self._dns_by_name: dict = {}

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise HetznerApiError(500, f"injected failure: {method}")

    def create_volume(self, req: VolumeCreateRequest) -> VolumeCreateResult:
        self.calls.append("create_volume")
        self._maybe_fail("create_volume")
        self.volumes.append(req)
        self._volume_n += 1
        return VolumeCreateResult(volume_id=f"vol_{self._volume_n}")

    def create_server(self, req: ServerCreateRequest) -> ServerCreateResult:
        self.calls.append("create_server")
        self._maybe_fail("create_server")
        self.servers.append(req)
        self._server_n += 1
        n = self._server_n
        return ServerCreateResult(
            server_id=f"server_{n}",
            public_ipv4=f"203.0.113.{n}",
            status="initializing",
        )

    def create_firewall(self, req: FirewallCreateRequest) -> FirewallCreateResult:
        self.calls.append("create_firewall")
        self._maybe_fail("create_firewall")
        self.firewalls.append(req)
        self._firewall_n += 1
        return FirewallCreateResult(firewall_id=f"fw_{self._firewall_n}")

    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult:
        self.calls.append("upsert_dns_record")
        self._maybe_fail("upsert_dns_record")
        self.dns.append(req)
        # True upsert: an existing (zone, name) A record keeps its id; a new one mints one.
        key = (req.zone_id, req.name)
        record_id = self._dns_by_name.get(key)
        if record_id is None:
            self._dns_n += 1
            record_id = f"dns_{self._dns_n}"
            self._dns_by_name[key] = record_id
        return DnsRecordResult(record_id=record_id, fqdn=req.name)

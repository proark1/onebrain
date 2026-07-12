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
    HetznerApiError,
    ServerCreateRequest,
    ServerCreateResult,
    VolumeCreateRequest,
    VolumeCreateResult,
)


class FakeHetznerClient:
    """In-memory Hetzner. Deterministic ids. `.servers`/`.volumes`/`.dns` hold the
    *Request objects seen; `.calls` is the ordered method-name log (ordering
    assertions). `fail_on={"create_server"}` raises HetznerApiError for that
    method."""

    def __init__(self, *, fail_on=frozenset()):
        self.fail_on = set(fail_on)
        self.servers: List[ServerCreateRequest] = []
        self.volumes: List[VolumeCreateRequest] = []
        self.dns: List[DnsRecordRequest] = []
        self.calls: List[str] = []
        self._server_n = 0
        self._volume_n = 0
        self._dns_n = 0

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

    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult:
        self.calls.append("upsert_dns_record")
        self._maybe_fail("upsert_dns_record")
        self.dns.append(req)
        self._dns_n += 1
        return DnsRecordResult(record_id=f"dns_{self._dns_n}", fqdn=req.name)

"""The transport-agnostic Hetzner Cloud client seam (P4-01).

`HetznerClient` is a Protocol; Phase 4 ships a real stdlib-urllib implementation
(`urllib_client.UrllibHetznerClient`, the ONLY module that talks to
api.hetzner.cloud) and an in-memory `fake.FakeHetznerClient`. Every provisioner
test runs against the fake — no live call is exercised in Phase 4 (a test may only
assert the real client's request SHAPE via an injected opener).

All request/result types are frozen dataclasses (house style). Destroy primitives
are DELIBERATELY absent from this seam: teardown/erasure execution is Phase-4-OUT
and a guarded delete lives on the broker, never here (P1-D — no single automated
un-protect+delete primitive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class HetznerApiError(RuntimeError):
    """A non-2xx response (or transport error) from the Hetzner Cloud API. Carries
    the HTTP status and a truncated, secret-free body (<=500 chars). The real
    client raises it; P4-03 maps it to a provisioning `dispatch_failed`, mirroring
    `dispatch_workflow`'s RuntimeError shape."""

    def __init__(self, status: int, body: str = ""):
        self.status = int(status)
        self.body = (body or "")[:500]
        super().__init__(f"Hetzner API error ({self.status}): {self.body}")


@dataclass(frozen=True)
class ServerCreateRequest:
    name: str
    server_type: str
    image: str
    location: str
    user_data: str                          # rendered cloud-init (P4-02)
    ssh_key_ids: tuple[int, ...] = ()
    firewall_ids: tuple[int, ...] = ()       # attached IN this create call (H-3) — never create-then-attach
    volume_ids: tuple[int, ...] = ()
    labels: dict = field(default_factory=dict)   # {"deployment_id": ..., "ring": ...}


@dataclass(frozen=True)
class ServerCreateResult:
    server_id: str                          # numeric id as string (D-6: railway_project_id = "hetzner:<server_id>")
    public_ipv4: str
    status: str                             # "initializing" | "running" | ...


@dataclass(frozen=True)
class VolumeCreateRequest:
    name: str
    size_gb: int
    location: str
    labels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VolumeCreateResult:
    volume_id: str


@dataclass(frozen=True)
class DnsRecordRequest:
    zone_id: str
    name: str                               # e.g. "<deployment_id>" (relative to the zone)
    ipv4: str
    ttl: int = 300


@dataclass(frozen=True)
class DnsRecordResult:
    record_id: str
    fqdn: str


class HetznerClient(Protocol):
    def create_volume(self, req: VolumeCreateRequest) -> VolumeCreateResult: ...

    def create_server(self, req: ServerCreateRequest) -> ServerCreateResult: ...

    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult: ...
    # Destroy primitives are DELIBERATELY not a single un-protect+delete (P1-D);
    # teardown execution is OUT of Phase 4. A guarded delete stub lives on the
    # broker, not here.

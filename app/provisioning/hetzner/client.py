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

# The constant fleet label stamped on EVERY server this control plane creates
# (in ADDITION to the per-deployment `deployment_id` + role labels). It is the
# ONLY selector the fleet-size circuit breaker counts, so a server that carries
# it is a billable box this control plane is responsible for. Defined once here
# (the client seam) so the provisioner AND the MC bootstrap runner stamp the
# exact same key/value the broker's cap check queries.
FLEET_LABEL_KEY = "managed-by"
FLEET_LABEL_VALUE = "onebrain-fleet"


class HetznerApiError(RuntimeError):
    """A non-2xx response (or transport error) from the Hetzner Cloud API. Carries
    the HTTP status and a truncated, secret-free body (<=500 chars). The real
    client raises it; P4-03 maps it to a provisioning `dispatch_failed`."""

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
    ssh_key_ids: tuple = ()                  # Hetzner SSH keys: int ids OR string names (API takes either)
    firewall_ids: tuple[int, ...] = ()       # attached IN this create call (H-3) — never create-then-attach
    volume_ids: tuple[int, ...] = ()
    labels: dict = field(default_factory=dict)   # {"deployment_id": ..., "ring": ...}


@dataclass(frozen=True)
class ServerCreateResult:
    server_id: str                          # numeric id as string (D-6: railway_project_id = "hetzner:<server_id>")
    public_ipv4: str
    status: str                             # "initializing" | "running" | ...


@dataclass(frozen=True)
class ServerActionResult:
    """The result of an async Hetzner server Action (e.g. enable_backup). We do NOT poll it
    to completion (mirrors create_server's no-readiness-poll contract); the Action finishes
    async and the box's later heartbeat is the real signal."""
    action_id: str                          # numeric Hetzner Action id as string ("" when the API returned none)
    status: str                             # "running" | "success" | "error" | "already_enabled"


@dataclass(frozen=True)
class ServerInfo:
    """A server as returned by `list_servers` (the cost-safety read seam). Carries the
    fields the broker's idempotency + fleet-cap gates need: the id/ip to reuse, the labels
    to match, and the status. A DELETED server is never returned (the gate must not reuse or
    count a torn-down box)."""
    id: str
    name: str
    labels: dict
    public_ipv4: str
    status: str


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
    zone_id: str                            # Cloud API zone id OR name (the rrset path accepts either)
    name: str                               # zone-RELATIVE label, e.g. "<deployment_id>" ("@" for the apex)
    ipv4: str
    ttl: int = 300


@dataclass(frozen=True)
class DnsRecordResult:
    record_id: str                          # RRSet key "<name>/A" (the model has no per-record id)
    fqdn: str


@dataclass(frozen=True)
class FirewallRule:
    """One inbound Hetzner Cloud Firewall rule. Hetzner firewalls are default-deny
    inbound: ONLY listed inbound rules are allowed. `port` is set for tcp/udp and
    omitted for icmp."""
    direction: str                                          # "in"
    protocol: str                                           # "tcp" | "icmp"
    port: str = ""                                          # "80" | "443" | "22" (tcp only)
    source_ips: tuple[str, ...] = ("0.0.0.0/0", "::/0")


@dataclass(frozen=True)
class FirewallCreateRequest:
    name: str
    rules: tuple[FirewallRule, ...]
    labels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FirewallCreateResult:
    firewall_id: str


class HetznerClient(Protocol):
    def create_volume(self, req: VolumeCreateRequest) -> VolumeCreateResult: ...

    def create_server(self, req: ServerCreateRequest) -> ServerCreateResult: ...

    def enable_backup(self, server_id: str) -> ServerActionResult: ...
    # Enable Hetzner's automated server Backups. ROOT-DISK IMAGE ONLY — never the attached
    # data volume that holds Postgres (/mnt/onebrain-data); this is whole-box convenience DR,
    # not the data-DR path (that is the offsite encrypted pg_dump, Part 2). Idempotent: a
    # server that already has backups enabled returns status="already_enabled", never raises.

    def list_servers(self, label_selector: str) -> list[ServerInfo]: ...
    # The cost-safety READ seam (no create). `label_selector` is a single Hetzner Cloud
    # `key=value` selector ("deployment_id=mc" for the idempotency gate, "managed-by=
    # onebrain-fleet" for the fleet-size cap). Returns only NON-deleted servers.

    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult: ...

    def create_firewall(self, req: FirewallCreateRequest) -> FirewallCreateResult: ...
    # Destroy primitives are DELIBERATELY not a single un-protect+delete (P1-D);
    # teardown execution is OUT of Phase 4. A guarded delete stub lives on the
    # broker, not here.

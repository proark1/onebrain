"""The transport-agnostic Hetzner Cloud client seam (P4-01).

`HetznerClient` is a Protocol; Phase 4 ships a real stdlib-urllib implementation
(`urllib_client.UrllibHetznerClient`, the ONLY module that talks to
api.hetzner.cloud) and an in-memory `fake.FakeHetznerClient`. Every provisioner
test runs against the fake — no live call is exercised in Phase 4 (a test may only
assert the real client's request SHAPE via an injected opener).

All request/result types are frozen dataclasses (house style). Delete primitives
(delete_server / volume / firewall / dns_record) exist for teardown, but there is
DELIBERATELY no single un-protect+delete primitive (P1-D): none of them clears
Hetzner delete-protection, and the guarded orchestration — discover the deployment's
resources by label, then delete — lives on the broker's `destroy_box`, never in a
caller."""

from __future__ import annotations

import hashlib
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


def provider_hostname_label(value: str) -> str:
    """Map a normalized deployment id to one stable RFC 1123 label — the box's DNS
    label under the fleet zone, and the piece teardown re-derives to find the DNS
    record (RRSets carry no labels, unlike servers/volumes/firewalls). This is the ONE
    definition shared by the provisioner (create) and the broker (destroy) so the two
    can never disagree on a box's hostname."""
    label = value.strip().lower().replace("_", "-").strip("-")
    if len(label) <= 63:
        return label
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{label[:54].rstrip('-')}-{digest}"


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
class VolumeInfo:
    """A volume as returned by `list_volumes` (the teardown scope-read seam). `server_id`
    is the server it is attached to ("" when detached); a delete must wait for detach."""
    id: str
    labels: dict
    server_id: str = ""


@dataclass(frozen=True)
class FirewallInfo:
    """A firewall as returned by `list_firewalls` (the teardown scope-read seam)."""
    id: str
    labels: dict


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

    # --- teardown scope reads + delete primitives (P1-D) -----------------------
    # Plain single-resource ops: NONE clears delete-protection, so there is no single
    # un-protect+delete primitive here. The broker's destroy_box discovers a deployment's
    # resources by label and orchestrates the guarded delete order.
    def list_volumes(self, label_selector: str) -> list[VolumeInfo]: ...

    def list_firewalls(self, label_selector: str) -> list[FirewallInfo]: ...

    def delete_server(self, server_id: str) -> None: ...

    def delete_volume(self, volume_id: str) -> None: ...
    # Real transport MUST detach an attached volume first (Hetzner refuses to delete an
    # attached volume). Idempotent: a missing volume (404) is a no-op.

    def delete_firewall(self, firewall_id: str) -> None: ...

    def delete_dns_record(self, zone_id: str, name: str) -> None: ...
    # Delete the zone's `<name>` A RRSet. Idempotent: a missing record (404) is a no-op.

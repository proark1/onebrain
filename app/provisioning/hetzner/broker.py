"""The token-isolating broker seam (P4-01, P1-D).

A `HetznerBroker` owns the Hetzner + DNS tokens and a `HetznerClient`; the
provisioner calls the broker, never the token or client directly. Phase 4 ships
`InProcessHetznerBroker` (the seam physically collapsed into the operator process);
Phase 5 replaces it with a `RemoteHetznerBroker` on its own host with its own token
(reached via `hetzner_broker_url`) behind the SAME Protocol — documented, not built.

The broker exposes CREATE primitives and a GUARDED destroy (explicit `confirm=True`,
and Phase-4 teardown execution is unimplemented) — deliberately NO single automated
un-protect+delete primitive (P1-D).

A6 invariant (encoded in `build_hetzner_broker`, not prose): a live in-process
broker holds the tokens inside the same internet-facing process that ingests
heartbeats — the fleet-wide-kill-switch exposure P1-D exists to prevent. So the
factory FORBIDS a live in-process broker when `provisioner_backend=="hetzner"`
unless `hetzner_allow_inprocess_broker=True` (dogfood/test only): production Hetzner
can only run through the out-of-process broker (`hetzner_broker_url`)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Protocol

from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    FirewallCreateRequest,
    HetznerClient,
    ServerCreateRequest,
    VolumeCreateRequest,
)


@dataclass(frozen=True)
class BrokerProvisionResult:
    server_id: str
    public_ipv4: str
    volume_ids: tuple[str, ...]
    dns_record_id: str
    fqdn: str
    firewall_id: str = ""     # id of a firewall CREATED in this flow ("" when a pre-existing one was attached)


class HetznerBroker(Protocol):
    def provision_box(
        self,
        *,
        server: ServerCreateRequest,
        volume: Optional[VolumeCreateRequest],
        dns: Optional[DnsRecordRequest],
        firewall: Optional[FirewallCreateRequest] = None,
    ) -> BrokerProvisionResult: ...

    def destroy_box(
        self,
        *,
        server_id: str,
        volume_ids: tuple[str, ...],
        dns_record_ids: tuple[str, ...],
        confirm: bool,
    ) -> None: ...
    # GUARDED (P1-D): confirm=True required; P4 raises NotImplementedError —
    # teardown execution is Phase-4-OUT.


class InProcessHetznerBroker:
    """P4: in-process. Holds a HetznerClient (constructed with the token by the
    factory); the provisioner never sees the token (the isolation SEAM, physically
    collapsed for P4). P5 replaces this with RemoteHetznerBroker (its own host, its
    own token, reached via hetzner_broker_url) behind the SAME Protocol."""

    def __init__(self, client: HetznerClient):
        self._client = client

    def provision_box(
        self,
        *,
        server: ServerCreateRequest,
        volume: Optional[VolumeCreateRequest] = None,
        dns: Optional[DnsRecordRequest] = None,
        firewall: Optional[FirewallCreateRequest] = None,
    ) -> BrokerProvisionResult:
        # 0. Firewall first (if the caller wants a fresh default-deny one) so its id is
        #    attached IN the server create call (H-3) — never create-then-attach. When
        #    the caller instead pinned a pre-created firewall (server.firewall_ids), no
        #    firewall is created and that id is used as-is.
        firewall_id = ""
        if firewall is not None:
            fw = self._client.create_firewall(firewall)
            firewall_id = fw.firewall_id
            server = replace(server, firewall_ids=tuple(server.firewall_ids) + (firewall_id,))
        # 1. Volume next (if requested) so its id can also be attached IN the create.
        volume_ids: tuple[str, ...] = ()
        if volume is not None:
            vol = self._client.create_volume(volume)
            volume_ids = (vol.volume_id,)
            server = replace(server, volume_ids=tuple(server.volume_ids) + (vol.volume_id,))
        # 2. Server WITH firewall + volume attached in the one create call.
        server_result = self._client.create_server(server)
        # 3. DNS last (if a provider was configured) — fill the A record's target
        #    from the freshly-minted server IP unless the caller pinned one.
        dns_record_id, fqdn = "", ""
        if dns is not None:
            resolved = dns if dns.ipv4 else replace(dns, ipv4=server_result.public_ipv4)
            dns_result = self._client.upsert_dns_record(resolved)
            dns_record_id, fqdn = dns_result.record_id, dns_result.fqdn
        return BrokerProvisionResult(
            server_id=server_result.server_id,
            public_ipv4=server_result.public_ipv4,
            volume_ids=volume_ids,
            dns_record_id=dns_record_id,
            fqdn=fqdn,
            firewall_id=firewall_id,
        )

    def destroy_box(
        self,
        *,
        server_id: str,
        volume_ids: tuple[str, ...],
        dns_record_ids: tuple[str, ...],
        confirm: bool,
    ) -> None:
        if not confirm:
            raise ValueError("destroy requires explicit confirm=True")
        raise NotImplementedError("teardown/erasure execution is Phase 4-OUT (architecture P4 ops)")


def build_hetzner_broker(settings, *, client: Optional[HetznerClient] = None) -> HetznerBroker:
    """Factory. Enforces the A6 isolation invariant in CODE:

    - `hetzner_broker_url` set -> the remote (out-of-process) broker is Phase 5 and
      its transport is not built; raise a clear error rather than silently no-op.
    - in-process path + `provisioner_backend=="hetzner"` + NOT
      `hetzner_allow_inprocess_broker` -> FORBIDDEN (production Hetzner must use the
      out-of-process broker); raise.
    - otherwise construct `InProcessHetznerBroker`. `client` lets tests inject a
      `FakeHetznerClient`; the guard still applies unless the dogfood flag is set."""
    if getattr(settings, "hetzner_broker_url", ""):
        raise RuntimeError(
            "remote Hetzner broker (hetzner_broker_url) is Phase 5; its transport is not built yet"
        )
    if settings.provisioner_backend == "hetzner" and not settings.hetzner_allow_inprocess_broker:
        raise RuntimeError(
            "live in-process Hetzner broker is forbidden in production; set hetzner_broker_url "
            "(out-of-process broker) or hetzner_allow_inprocess_broker=True for dogfood/test"
        )
    if client is None:
        from app.provisioning.hetzner.urllib_client import UrllibHetznerClient

        client = UrllibHetznerClient(settings.hetzner_api_token, settings.fleet_dns_token)
    return InProcessHetznerBroker(client)

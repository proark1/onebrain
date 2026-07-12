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

import logging
from dataclasses import dataclass, replace
from typing import Optional, Protocol

from app.provisioning.hetzner.client import (
    FLEET_LABEL_KEY,
    FLEET_LABEL_VALUE,
    DnsRecordRequest,
    FirewallCreateRequest,
    HetznerClient,
    ServerCreateRequest,
    VolumeCreateRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerProvisionResult:
    server_id: str
    public_ipv4: str
    volume_ids: tuple[str, ...]
    dns_record_id: str
    fqdn: str
    firewall_id: str = ""     # id of a firewall CREATED in this flow ("" when a pre-existing one was attached)
    reused: bool = False      # True when the idempotency gate returned a PRE-EXISTING server (nothing was created)


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

    def __init__(self, client: HetznerClient, *, max_fleet_servers: int = 0):
        self._client = client
        # The fleet-size COST CIRCUIT BREAKER cap (settings.hetzner_max_fleet_servers,
        # threaded by build_hetzner_broker). <=0 DISABLES the breaker — used only by the
        # direct-construction unit tests that are not exercising the cap; every production
        # path goes through the factory, which always threads the real (default 5) cap.
        self._max_fleet_servers = int(max_fleet_servers or 0)

    def provision_box(
        self,
        *,
        server: ServerCreateRequest,
        volume: Optional[VolumeCreateRequest] = None,
        dns: Optional[DnsRecordRequest] = None,
        firewall: Optional[FirewallCreateRequest] = None,
    ) -> BrokerProvisionResult:
        # ===================================================================
        # COST-SAFETY GATEKEEPER (runs BEFORE any billable create call).
        # Nothing else in the fleet stops duplicate/runaway server creation, so
        # both gates live here in provision_box — the ONE chokepoint every caller
        # (the provisioner AND scripts/bootstrap_mc.py) funnels through, making
        # them unbypassable.
        # ===================================================================
        # GATE 1 — IDEMPOTENCY (exactly one server per deployment). If a non-deleted
        #   server already carries this deployment_id label, REUSE it: create nothing,
        #   return the existing id/ip. Makes provisioning safe to retry infinitely
        #   (a retry, double-dispatch, or replayed callback never mints a second box).
        deployment_id = str((server.labels or {}).get("deployment_id", "")).strip()
        if deployment_id:
            existing = self._client.list_servers(f"deployment_id={deployment_id}")
            if existing:
                found = existing[0]
                logger.info(
                    "reusing existing server %s for deployment %s (idempotent)",
                    found.id, deployment_id,
                )
                # firewall_id / dns_record_id / volume_ids are left empty exactly like a
                # pre-existing-firewall attach: nothing was created in THIS flow. fqdn is
                # reconstructed from the DNS request name so an idempotent reuse still
                # surfaces the box hostname to the caller (e.g. the MC bootstrap runner).
                return BrokerProvisionResult(
                    server_id=found.id,
                    public_ipv4=found.public_ipv4,
                    volume_ids=(),
                    dns_record_id="",
                    fqdn=(dns.name if dns is not None else ""),
                    firewall_id="",
                    reused=True,
                )
        # GATE 2 — FLEET-SIZE CIRCUIT BREAKER. Runs AFTER the idempotency check (so a
        #   reuse never trips it) and BEFORE any create. Count only fleet-labelled servers
        #   (the boxes this control plane is billed for) and refuse to grow past the cap.
        if self._max_fleet_servers > 0:
            fleet = self._client.list_servers(f"{FLEET_LABEL_KEY}={FLEET_LABEL_VALUE}")
            count = len(fleet)
            if count >= self._max_fleet_servers:
                raise RuntimeError(
                    f"fleet server cap reached ({count}/{self._max_fleet_servers}): refusing to "
                    "create a new server; raise ONEBRAIN_HETZNER_MAX_FLEET_SERVERS to grow the fleet."
                )
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

        # ONE Cloud token covers compute AND DNS (unified Cloud API, GA 2025-11-10).
        client = UrllibHetznerClient(settings.hetzner_api_token)
    # Thread the fleet-size cost cap so the circuit breaker is enforced inside
    # provision_box for EVERY factory-built broker — the provisioner AND bootstrap_mc.
    return InProcessHetznerBroker(
        client, max_fleet_servers=getattr(settings, "hetzner_max_fleet_servers", 0)
    )

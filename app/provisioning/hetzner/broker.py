"""The token-isolating broker seam (P4-01, P1-D).

A `HetznerBroker` owns the Hetzner + DNS token boundary; the provisioner calls
the broker, never the token or cloud client directly. The in-process broker is
for tests and explicit dogfood only. Production uses `RemoteHetznerBroker` on a
dedicated host, reached through `hetzner_broker_url` over mTLS.

The broker exposes CREATE primitives and a GUARDED destroy (`destroy_box`, explicit
`confirm=True`): given a deployment id it DISCOVERS that deployment's own labelled
resources and deletes them — deliberately NO single automated un-protect+delete
primitive (P1-D), and no way to point it at a resource id it did not discover.

A6 invariant (encoded in `build_hetzner_broker`, not prose): a live in-process
broker holds the tokens inside the same internet-facing process that ingests
heartbeats — the fleet-wide-kill-switch exposure P1-D exists to prevent. So the
factory FORBIDS a live in-process broker when `provisioner_backend=="hetzner"`
unless `hetzner_allow_inprocess_broker=True` (dogfood/test only): production Hetzner
can only run through the out-of-process broker (`hetzner_broker_url`)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Optional, Protocol

from app.provisioning.hetzner.client import (
    FLEET_LABEL_KEY,
    FLEET_LABEL_VALUE,
    DnsRecordRequest,
    FirewallCreateRequest,
    HetznerApiError,
    HetznerClient,
    ServerCreateRequest,
    VolumeCreateRequest,
    provider_hostname_label,
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
    backups_enabled: bool = False   # whether the broker requested Hetzner server Backups (root-disk only) for this box


@dataclass(frozen=True)
class BrokerDestroyResult:
    """The outcome of a guarded teardown: the ids the broker actually deleted (each
    DISCOVERED by the deployment's own label), plus `nothing_found` — True when no
    server/volume/firewall remained, i.e. MC should tombstone the record as
    "no infrastructure touched" rather than reporting a destroy."""
    deployment_id: str
    servers_deleted: tuple[str, ...] = ()
    volumes_deleted: tuple[str, ...] = ()
    firewalls_deleted: tuple[str, ...] = ()
    dns_deleted: tuple[str, ...] = ()
    nothing_found: bool = False


class HetznerBroker(Protocol):
    def provision_box(
        self,
        *,
        server: ServerCreateRequest,
        volume: Optional[VolumeCreateRequest],
        dns: Optional[DnsRecordRequest],
        firewall: Optional[FirewallCreateRequest] = None,
    ) -> BrokerProvisionResult: ...

    def destroy_box(self, deployment_id: str, *, confirm: bool) -> "BrokerDestroyResult": ...
    # GUARDED (P1-D): confirm=True required. Discovers the deployment's own resources by
    # label and deletes them — no single un-protect+delete primitive. Idempotent/re-runnable.


class InProcessHetznerBroker:
    """Test/dogfood implementation. It holds a client locally, so production
    factory wiring rejects it unless the explicit dogfood escape hatch is set."""

    def __init__(self, client: HetznerClient, *, max_fleet_servers: int = 0,
                 enable_backups: bool = False, dns_zone_id: str = ""):
        self._client = client
        # Fleet DNS zone (Hetzner Cloud zone id or name), threaded from settings so teardown
        # can delete the box's A RRSet — RRSets carry no labels, so it is re-derived by name.
        self._dns_zone_id = str(dns_zone_id or "").strip()
        # Overridable sleeper for the volume detach-retry window (tests inject a no-op).
        self._sleep = time.sleep
        # The fleet-size COST CIRCUIT BREAKER cap (settings.hetzner_max_fleet_servers,
        # threaded by build_hetzner_broker). <=0 DISABLES the breaker — used only by the
        # direct-construction unit tests that are not exercising the cap; every production
        # path goes through the factory, which always threads the real (default 5) cap.
        self._max_fleet_servers = int(max_fleet_servers or 0)
        # Whether to enable Hetzner server Backups after create (settings.hetzner_enable_backups,
        # threaded by build_hetzner_broker; default False on direct construction like the cap).
        # NOTE: Hetzner server Backups image the ROOT DISK ONLY — never the attached data volume
        # that holds Postgres (/mnt/onebrain-data). This is whole-box convenience DR (fast OS/root
        # rebuild); the authoritative DATABASE DR is the offsite encrypted pg_dump path
        # (Part 2 / onebrain_backup.sh), NOT this.
        self._enable_backups = bool(enable_backups)

    def _maybe_enable_backups(self, server_id: str) -> bool:
        """Enable Hetzner Backups when configured; NON-FATAL on failure (a box that boots but
        lacks root-disk backups beats a failed provision, and Part 2 is the real data DR).
        Idempotent on the client side (already_enabled). Returns whether it was requested."""
        if not self._enable_backups:
            return False
        try:
            self._client.enable_backup(server_id)
        except HetznerApiError as exc:
            logger.warning("enable_backup failed for %s (continuing): %s", server_id, exc)
        return True

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
                # Converge a reused box (or one created before this feature) to backups-enabled;
                # already_enabled makes it a safe no-op.
                reused_backups = self._maybe_enable_backups(found.id)
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
                    backups_enabled=reused_backups,
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
        # 2b. Enable Hetzner server Backups (root-disk only) right after create, BEFORE DNS.
        #     The server is transiently locked while the action runs, but DNS is a separate
        #     resource and we issue no dependent call on the server here, so the interleave is
        #     safe. Non-fatal (see _maybe_enable_backups).
        backups_enabled = self._maybe_enable_backups(server_result.server_id)
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
            backups_enabled=backups_enabled,
        )

    def destroy_box(self, deployment_id: str, *, confirm: bool) -> BrokerDestroyResult:
        # GUARDED teardown (P1-D). We NEVER delete an id we were merely handed: the broker
        # DISCOVERS the deployment's own resources by label and deletes exactly those. That
        # single rule delivers per-resource scope (a foreign volume/firewall/DNS record can't
        # be reached) AND completeness for idempotent-reuse boxes (whose later run's manifest
        # is empty). No step clears delete-protection, so there is no un-protect+delete.
        if not confirm:
            raise ValueError("destroy requires explicit confirm=True")
        deployment_id = str(deployment_id or "").strip()
        if not deployment_id:
            raise ValueError("destroy requires a deployment_id")

        # A server must carry BOTH the deployment id AND the fleet label (a box this control
        # plane created). List by deployment_id, then require the fleet label in-code — one
        # single-label read keeps the seam simple and the fleet-label scope check explicit.
        candidates = self._client.list_servers(f"deployment_id={deployment_id}")
        servers = [s for s in candidates
                   if (s.labels or {}).get(FLEET_LABEL_KEY) == FLEET_LABEL_VALUE]
        # Volumes and firewalls are scoped the SAME way. deployment_id is a generic label, so a
        # resource is ours only if it ALSO carries the fleet ownership label; one that merely
        # shares the deployment_id (not stamped by our provisioner) is never touched.
        volumes = [v for v in self._client.list_volumes(f"deployment_id={deployment_id}")
                   if (v.labels or {}).get(FLEET_LABEL_KEY) == FLEET_LABEL_VALUE]
        firewalls = [f for f in self._client.list_firewalls(f"deployment_id={deployment_id}")
                     if (f.labels or {}).get(FLEET_LABEL_KEY) == FLEET_LABEL_VALUE]

        # 1. Servers first — deleting a server detaches its volumes.
        servers_deleted = []
        for server in servers:
            self._client.delete_server(server.id)
            servers_deleted.append(server.id)
        # 2. Volumes next, tolerating the brief post-delete detach window.
        volumes_deleted = []
        for volume in volumes:
            self._delete_volume_when_detached(volume.id)
            volumes_deleted.append(volume.id)
        # 3. Firewalls (no longer applied to a live server).
        firewalls_deleted = []
        for firewall in firewalls:
            self._client.delete_firewall(firewall.id)
            firewalls_deleted.append(firewall.id)
        # 4. DNS last. RRSets carry no labels, so the record is re-derived by hostname — which
        #    means it must be scoped by DISCOVERY, not by the raw id. Delete only when a fleet
        #    resource was actually found for this deployment AND the derived label is non-empty:
        #    an underscore-only id ("___" -> "") would otherwise resolve to the zone apex, and any
        #    name would be deletable for a deployment that owns nothing (an unscoped DNS delete).
        dns_deleted = []
        if self._dns_zone_id and (servers or volumes or firewalls):
            name = provider_hostname_label(deployment_id)
            if name:
                self._client.delete_dns_record(self._dns_zone_id, name)
                dns_deleted.append(f"{name}/A")

        return BrokerDestroyResult(
            deployment_id=deployment_id,
            servers_deleted=tuple(servers_deleted),
            volumes_deleted=tuple(volumes_deleted),
            firewalls_deleted=tuple(firewalls_deleted),
            dns_deleted=tuple(dns_deleted),
            # Record-only signal for MC: no billable/data resource remained for this box.
            nothing_found=not (servers or volumes or firewalls),
        )

    def _delete_volume_when_detached(self, volume_id: str, *, attempts: int = 5) -> None:
        """Delete a volume, tolerating the brief window after a server delete where Hetzner
        may still report it attached (HTTP 409). The server delete detaches it; retry a few
        times, then give up (teardown is idempotent — a re-run finishes any straggler)."""
        last: Optional[HetznerApiError] = None
        for attempt in range(max(1, attempts)):
            try:
                self._client.delete_volume(volume_id)
                return
            except HetznerApiError as exc:
                if exc.status != 409:
                    raise
                last = exc
                if attempt < attempts - 1:
                    self._sleep(1.0)
        if last is not None:
            raise last


def build_hetzner_broker(settings, *, client: Optional[HetznerClient] = None) -> HetznerBroker:
    """Factory. Enforces the A6 isolation invariant in CODE:

    - `hetzner_broker_url` set -> use the dedicated remote broker. MC supplies
      mTLS client material and a broker credential, never a Hetzner API token.
    - in-process path + `provisioner_backend=="hetzner"` + NOT
      `hetzner_allow_inprocess_broker` -> FORBIDDEN (production Hetzner must use the
      out-of-process broker); raise.
    - otherwise construct `InProcessHetznerBroker`. `client` lets tests inject a
      `FakeHetznerClient`; the guard still applies unless the dogfood flag is set."""
    if getattr(settings, "hetzner_broker_url", ""):
        if getattr(settings, "hetzner_api_token", ""):
            raise RuntimeError("Mission Control must not hold ONEBRAIN_HETZNER_API_TOKEN when using a remote broker")
        from app.provisioning.hetzner.remote import RemoteHetznerBroker

        return RemoteHetznerBroker(
            settings.hetzner_broker_url,
            getattr(settings, "hetzner_broker_credential", ""),
            client_certificate_file=getattr(settings, "hetzner_broker_client_certificate_file", ""),
            client_key_file=getattr(settings, "hetzner_broker_client_key_file", ""),
            ca_file=getattr(settings, "hetzner_broker_ca_file", ""),
            timeout_seconds=float(getattr(settings, "hetzner_broker_timeout_seconds", 10.0)),
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
        client,
        max_fleet_servers=getattr(settings, "hetzner_max_fleet_servers", 0),
        enable_backups=getattr(settings, "hetzner_enable_backups", True),
        dns_zone_id=getattr(settings, "fleet_dns_zone_id", ""),
    )

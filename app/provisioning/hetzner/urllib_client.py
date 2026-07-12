"""The real Hetzner Cloud client (P4-01) — the ONLY module that talks to
api.hetzner.cloud. stdlib `urllib`, an injectable `opener(request, timeout)` so
tests need no network (the exact seam as `app.provisioning.runs.dispatch_workflow`
and `app.fleet.reporter.send_heartbeat`). The token is passed IN at construction
by the broker; this class NEVER imports `get_settings` or reads a global.

Phase 4 wires this up but exercises it only via an injected opener (request-SHAPE
assertions); the live call is the Phase-5 step. No retries, no readiness polling —
a create returns an id immediately; readiness is the box cloud-init callback's job
(P4-03)."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

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

_TIMEOUT_SECONDS = 20


class UrllibHetznerClient:
    """The ONLY module that talks to api.hetzner.cloud. stdlib urllib, injectable
    opener (tests need no network — same seam as dispatch_workflow/send_heartbeat).
    The token is passed IN by the broker; this class never imports get_settings.

    ONE Bearer token covers everything — servers, firewalls, volumes AND DNS. DNS was
    folded into the unified Cloud API (GA 2025-11-10); the legacy dns.hetzner.com host +
    `Auth-API-Token` header is gone, so there is no separate DNS token or base URL."""

    def __init__(
        self,
        api_token: str,
        *,
        opener=None,
        base: str = "https://api.hetzner.cloud/v1",
    ):
        self._api_token = api_token
        self._base = base.rstrip("/")
        self._opener = opener

    # --- transport -------------------------------------------------------------
    @staticmethod
    def _error_body(exc: HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", "replace")
        except Exception:
            return getattr(exc, "reason", "") or ""

    def _do_open(self, request: Request):
        do_open = self._opener or (lambda req, timeout: urlopen(req, timeout=timeout))
        return do_open(request, _TIMEOUT_SECONDS)

    def _headers(self, *, with_content: bool) -> dict:
        # The SAME Bearer token as compute — never the legacy `Auth-API-Token` header.
        headers = {"Authorization": f"Bearer {self._api_token}", "Accept": "application/json"}
        if with_content:
            headers["Content-Type"] = "application/json"
        return headers

    def _open(self, request: Request) -> dict:
        try:
            with self._do_open(request) as response:
                raw = response.read()
        except HTTPError as exc:
            raise HetznerApiError(exc.code, self._error_body(exc)) from exc
        except URLError as exc:
            raise HetznerApiError(0, str(getattr(exc, "reason", exc))) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _post(self, path: str, body: dict) -> dict:
        request = Request(
            self._base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=self._headers(with_content=True),
        )
        return self._open(request)

    # --- compute ---------------------------------------------------------------
    def create_volume(self, req: VolumeCreateRequest) -> VolumeCreateResult:
        body: dict = {
            "name": req.name,
            "size": int(req.size_gb),
            "location": req.location,
            "format": "ext4",
        }
        if req.labels:
            body["labels"] = {str(k): str(v) for k, v in req.labels.items()}
        data = self._post("/volumes", body)
        volume = data.get("volume", {}) or {}
        return VolumeCreateResult(volume_id=str(volume.get("id", "")))

    def create_server(self, req: ServerCreateRequest) -> ServerCreateResult:
        body: dict = {
            "name": req.name,
            "server_type": req.server_type,
            "image": req.image,
            "location": req.location,
            "user_data": req.user_data,
            "start_after_create": True,
        }
        if req.ssh_key_ids:
            body["ssh_keys"] = list(req.ssh_key_ids)
        if req.firewall_ids:
            # H-3: firewall attached IN the create body — never create-then-attach.
            body["firewalls"] = [{"firewall": int(fid)} for fid in req.firewall_ids]
        if req.volume_ids:
            body["volumes"] = list(req.volume_ids)
            body["automount"] = False
        if req.labels:
            body["labels"] = {str(k): str(v) for k, v in req.labels.items()}
        data = self._post("/servers", body)
        server = data.get("server", {}) or {}
        public_net = server.get("public_net", {}) or {}
        ipv4 = (public_net.get("ipv4") or {}).get("ip", "")
        return ServerCreateResult(
            server_id=str(server.get("id", "")),
            public_ipv4=ipv4,
            status=server.get("status", "initializing"),
        )

    def create_firewall(self, req: FirewallCreateRequest) -> FirewallCreateResult:
        # Hetzner Cloud firewalls are default-deny INBOUND: only the listed inbound
        # rules are allowed. Egress is unrestricted (the metadata-egress block is done
        # at the box iptables layer). A tcp/udp rule carries a port; icmp does not.
        rules = []
        for rule in req.rules:
            entry = {"direction": rule.direction, "protocol": rule.protocol,
                     "source_ips": list(rule.source_ips)}
            if rule.port:
                entry["port"] = rule.port
            rules.append(entry)
        body: dict = {"name": req.name, "rules": rules}
        if req.labels:
            body["labels"] = {str(k): str(v) for k, v in req.labels.items()}
        data = self._post("/firewalls", body)
        firewall = data.get("firewall", {}) or {}
        return FirewallCreateResult(firewall_id=str(firewall.get("id", "")))

    # --- DNS (unified Cloud API, RRSet model; SAME host + Bearer token as compute) ---
    # DNS was folded into the Cloud API (GA 2025-11-10): the legacy dns.hetzner.com +
    # `Auth-API-Token` path is gone, and a Cloud token (Bearer) now authenticates DNS too.
    # Records are RRSets keyed by (zone-relative name, type), each carrying an ARRAY of
    # values — there is NO per-record id; you address by name+type. The zone path segment
    # accepts the zone id OR its name, so `req.zone_id` (settings.fleet_dns_zone_id) can be
    # either and no separate zone-id lookup is needed.
    def _rrset_exists(self, zone: str, label: str) -> bool:
        """Probe whether the zone's `<label>` A RRSet already exists — the idempotency
        signal for the upsert. 200 -> True, 404 -> False; any other status raises. (If this
        exact single-RRSet GET path ever 405s, list via GET /zones/{zone}/rrsets?name&type.)"""
        request = Request(
            f"{self._base}/zones/{zone}/rrsets/{label}/A",
            method="GET",
            headers=self._headers(with_content=False),
        )
        try:
            with self._do_open(request) as response:
                response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return False
            raise HetznerApiError(exc.code, self._error_body(exc)) from exc
        except URLError as exc:
            raise HetznerApiError(0, str(getattr(exc, "reason", exc))) from exc
        return True

    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult:
        """Idempotent create-or-update of the zone's A RRSet for `req.name` (a zone-RELATIVE
        label; the apex is "@"). Probe the (name, A) RRSet, then either POST a new RRSet or
        REPLACE its value list via the set_records action."""
        zone = quote(req.zone_id, safe="")
        # Zone-RELATIVE label; empty normalizes to the apex "@" (URL-encoded as %40).
        label = quote(req.name or "@", safe="")
        record = {"value": req.ipv4}
        if self._rrset_exists(zone, label):
            # set_records REPLACES the full value list, so it is naturally idempotent — the
            # primary 'update the value(s)' call. (change_ttl handles TTL-only edits; a
            # value-only upsert never needs it — the ttl was baked at create.)
            self._post(f"/zones/{zone}/rrsets/{label}/A/actions/set_records", {"records": [record]})
        else:
            # Create the RRSet: `name` is the zone-RELATIVE label, `records` the value array.
            self._post(
                f"/zones/{zone}/rrsets",
                {"name": req.name or "@", "type": "A", "ttl": int(req.ttl), "records": [record]},
            )
        # The RRSet has no per-record id — it is addressed by name+type; that key is the
        # stable identifier recorded for teardown (DELETE /zones/{zone}/rrsets/{name}/A).
        return DnsRecordResult(record_id=f"{req.name or '@'}/A", fqdn=req.name)

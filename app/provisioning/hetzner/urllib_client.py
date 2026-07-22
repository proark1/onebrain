"""The real Hetzner Cloud client (P4-01) — the only module that talks to
api.hetzner.cloud. It uses stdlib `urllib` with an injectable
`opener(request, timeout)` so tests need no network. The token is passed in at construction
by the broker; this class NEVER imports `get_settings` or reads a global.

Phase 4 wires this up but exercises it only via an injected opener (request-SHAPE
assertions); the live call is the Phase-5 step. No retries, no readiness polling —
a create returns an id immediately; readiness is the box cloud-init callback's job
(P4-03)."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    DnsRecordResult,
    FirewallCreateRequest,
    FirewallCreateResult,
    FirewallInfo,
    HetznerApiError,
    ServerActionResult,
    ServerCreateRequest,
    ServerCreateResult,
    ServerInfo,
    VolumeCreateRequest,
    VolumeCreateResult,
    VolumeInfo,
)

# Hetzner error code (and HTTP statuses) that mean "backups are already on" — an
# idempotent no-op, not a failure.
_BACKUP_ALREADY = "server_backup_already_enabled"

_TIMEOUT_SECONDS = 20


class UrllibHetznerClient:
    """The ONLY module that talks to api.hetzner.cloud. stdlib urllib, injectable
    opener (tests need no network).
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

    def _delete(self, path: str) -> None:
        """DELETE <path>. A 404 is an idempotent no-op (already gone); every other non-2xx
        raises HetznerApiError — a 409 propagates so the broker can retry an attached-volume
        delete. DELETE returns an async Action for some resources; we do not poll it."""
        request = Request(self._base + path, method="DELETE", headers=self._headers(with_content=False))
        try:
            with self._do_open(request) as response:
                response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return
            raise HetznerApiError(exc.code, self._error_body(exc)) from exc
        except URLError as exc:
            raise HetznerApiError(0, str(getattr(exc, "reason", exc))) from exc

    def _paged_by_label(self, resource: str, label_selector: str):
        """GET /<resource>?label_selector=<sel>, following pagination; yields each entry dict.
        The teardown scope-read seam for volumes/firewalls (mirrors list_servers)."""
        page = 1
        while True:
            query = urlencode({"label_selector": label_selector, "page": page, "per_page": 50})
            request = Request(f"{self._base}/{resource}?{query}", method="GET",
                              headers=self._headers(with_content=False))
            data = self._open(request)
            for entry in data.get(resource, []) or []:
                yield entry
            next_page = ((data.get("meta", {}) or {}).get("pagination", {}) or {}).get("next_page")
            if not next_page:
                return
            page = next_page

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

    def enable_backup(self, server_id: str) -> ServerActionResult:
        """POST /servers/{id}/actions/enable_backup (empty body — Hetzner auto-selects the
        backup window; the deprecated `backup_window` is not sent). Returns the async Action
        WITHOUT polling it to completion (mirrors create_server). Idempotent: a server that
        already has backups on returns status="already_enabled" instead of raising."""
        try:
            data = self._post(f"/servers/{quote(str(server_id), safe='')}/actions/enable_backup", {})
        except HetznerApiError as exc:
            # Already-enabled is a no-op, not a failure. Hetzner signals it with the
            # `server_backup_already_enabled` code (HTTP 409/422/423). Match on the body code
            # so we never mistake a genuine failure (500, auth, not-found) for idempotency.
            if _BACKUP_ALREADY in (exc.body or "") and exc.status in (409, 422, 423):
                return ServerActionResult(action_id="", status="already_enabled")
            raise
        action = data.get("action", {}) or {}
        return ServerActionResult(action_id=str(action.get("id", "")), status=action.get("status", "running"))

    def list_servers(self, label_selector: str) -> list[ServerInfo]:
        """GET /servers?label_selector=<selector> (the cost-safety read seam). Bearer auth,
        same host/token as compute. Pages FULLY (per_page=50) so the fleet-size cap can never
        be undercounted by a truncated first page. The API only returns live servers, so a
        deleted box never appears — exactly what the idempotency + cap gates require."""
        servers: list[ServerInfo] = []
        page = 1
        while True:
            query = urlencode({"label_selector": label_selector, "page": page, "per_page": 50})
            request = Request(
                f"{self._base}/servers?{query}",
                method="GET",
                headers=self._headers(with_content=False),
            )
            data = self._open(request)
            for entry in data.get("servers", []) or []:
                entry_net = entry.get("public_net", {}) or {}
                entry_ipv4 = (entry_net.get("ipv4") or {}).get("ip", "")
                servers.append(ServerInfo(
                    id=str(entry.get("id", "")),
                    name=entry.get("name", "") or "",
                    labels=dict(entry.get("labels", {}) or {}),
                    public_ipv4=entry_ipv4,
                    status=entry.get("status", "") or "",
                ))
            next_page = ((data.get("meta", {}) or {}).get("pagination", {}) or {}).get("next_page")
            if not next_page:
                break
            page = next_page
        return servers

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

    # --- teardown scope reads + delete primitives (Phase A) --------------------
    def list_volumes(self, label_selector: str) -> list[VolumeInfo]:
        return [
            VolumeInfo(
                id=str(entry.get("id", "")),
                labels=dict(entry.get("labels", {}) or {}),
                server_id=str(entry.get("server") or ""),   # attached server id (null when detached)
            )
            for entry in self._paged_by_label("volumes", label_selector)
        ]

    def list_firewalls(self, label_selector: str) -> list[FirewallInfo]:
        return [
            FirewallInfo(id=str(entry.get("id", "")), labels=dict(entry.get("labels", {}) or {}))
            for entry in self._paged_by_label("firewalls", label_selector)
        ]

    def delete_server(self, server_id: str) -> None:
        self._delete(f"/servers/{quote(str(server_id), safe='')}")

    def delete_volume(self, volume_id: str) -> None:
        # The server delete detaches the volume; Hetzner still 409s until detach lands, which
        # the broker's destroy retries. A missing volume (404) is an idempotent no-op.
        self._delete(f"/volumes/{quote(str(volume_id), safe='')}")

    def delete_firewall(self, firewall_id: str) -> None:
        self._delete(f"/firewalls/{quote(str(firewall_id), safe='')}")

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

    def delete_dns_record(self, zone_id: str, name: str) -> None:
        # DELETE the zone's `<name>` A RRSet (zone-relative name + type). Empty name is the
        # apex "@". A missing record (404) is an idempotent no-op.
        zone = quote(zone_id, safe="")
        label = quote(name or "@", safe="")
        self._delete(f"/zones/{zone}/rrsets/{label}/A")

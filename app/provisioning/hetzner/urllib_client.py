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
from urllib.parse import urlencode
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
    The token is passed IN by the broker; this class never imports get_settings."""

    def __init__(
        self,
        api_token: str,
        dns_token: str = "",
        *,
        opener=None,
        base: str = "https://api.hetzner.cloud/v1",
        dns_base: str = "https://dns.hetzner.com/api/v1",
    ):
        self._api_token = api_token
        self._dns_token = dns_token
        self._base = base.rstrip("/")
        self._dns_base = dns_base.rstrip("/")
        self._opener = opener

    # --- transport -------------------------------------------------------------
    def _open(self, request: Request) -> dict:
        do_open = self._opener or (lambda req, timeout: urlopen(req, timeout=timeout))
        try:
            with do_open(request, _TIMEOUT_SECONDS) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = getattr(exc, "reason", "") or ""
            raise HetznerApiError(exc.code, body) from exc
        except URLError as exc:
            raise HetznerApiError(0, str(getattr(exc, "reason", exc))) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _post(self, path: str, body: dict, *, token: str) -> dict:
        request = Request(
            self._base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
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
        data = self._post("/volumes", body, token=self._api_token)
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
        data = self._post("/servers", body, token=self._api_token)
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
        data = self._post("/firewalls", body, token=self._api_token)
        firewall = data.get("firewall", {}) or {}
        return FirewallCreateResult(firewall_id=str(firewall.get("id", "")))

    # --- DNS (separate host + token; Bearer is not used here) -------------------
    def _dns_headers(self, *, with_content: bool = True) -> dict:
        headers = {"Auth-API-Token": self._dns_token, "Accept": "application/json"}
        if with_content:
            headers["Content-Type"] = "application/json"
        return headers

    def _find_dns_record(self, zone_id: str, name: str) -> str:
        """The id of an existing A record with this name in the zone, or "". Makes the
        upsert a TRUE upsert (a re-provision / IP change updates the record instead of
        creating a duplicate A record)."""
        query = urlencode({"zone_id": zone_id})
        request = Request(self._dns_base + f"/records?{query}", method="GET",
                          headers=self._dns_headers(with_content=False))
        data = self._open(request)
        for record in data.get("records", []) or []:
            if record.get("type") == "A" and record.get("name") == name:
                return str(record.get("id", ""))
        return ""

    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult:
        body = {
            "zone_id": req.zone_id,
            "type": "A",
            "name": req.name,
            "value": req.ipv4,
            "ttl": int(req.ttl),
        }
        # True upsert: PUT an existing A record (same zone + name), else POST a new one.
        existing_id = self._find_dns_record(req.zone_id, req.name)
        if existing_id:
            request = Request(self._dns_base + f"/records/{existing_id}",
                              data=json.dumps(body).encode("utf-8"), method="PUT",
                              headers=self._dns_headers())
        else:
            request = Request(self._dns_base + "/records",
                              data=json.dumps(body).encode("utf-8"), method="POST",
                              headers=self._dns_headers())
        data = self._open(request)
        record = data.get("record", {}) or {}
        record_id = str(record.get("id", "") or existing_id)
        zone = record.get("zone_name") or ""
        name = record.get("name") or req.name
        fqdn = record.get("fqdn") or (f"{name}.{zone}".rstrip(".") if zone else name)
        return DnsRecordResult(record_id=record_id, fqdn=fqdn)

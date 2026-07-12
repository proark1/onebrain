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
from urllib.request import Request, urlopen

from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    DnsRecordResult,
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

    # --- DNS (separate host + token; Bearer is not used here) -------------------
    def upsert_dns_record(self, req: DnsRecordRequest) -> DnsRecordResult:
        body = {
            "zone_id": req.zone_id,
            "type": "A",
            "name": req.name,
            "value": req.ipv4,
            "ttl": int(req.ttl),
        }
        request = Request(
            self._dns_base + "/records",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Auth-API-Token": self._dns_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        data = self._open(request)
        record = data.get("record", {}) or {}
        record_id = str(record.get("id", ""))
        zone = record.get("zone_name") or ""
        name = record.get("name") or req.name
        fqdn = record.get("fqdn") or (f"{name}.{zone}".rstrip(".") if zone else name)
        return DnsRecordResult(record_id=record_id, fqdn=fqdn)

"""Private, token-owning Hetzner broker service.

Run this app only on the dedicated broker host behind a mutually authenticated
TLS proxy. It has no OneBrain database, customer routes, fleet routes, or UI.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.provisioning.hetzner.broker import InProcessHetznerBroker
from app.provisioning.hetzner.client import FLEET_LABEL_KEY, FLEET_LABEL_VALUE, FirewallRule
from app.provisioning.hetzner.remote import (
    decode_provision_request,
    encode_provision_result,
)
from app.provisioning.hetzner.urllib_client import UrllibHetznerClient
from app.servicekeys.base import verify_secret

logger = logging.getLogger(__name__)

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DEPLOYMENT_ID = re.compile(r"^[a-z0-9_]{1,120}$")
_PUBLIC_SOURCES = ("0.0.0.0/0", "::/0")


class BrokerSettings(BaseSettings):
    """Broker-only settings. No `ONEBRAIN_` application secret is accepted here."""

    model_config = SettingsConfigDict(env_prefix="HETZNER_BROKER_", env_file=None, extra="ignore")

    api_token: str = ""
    mc_token_hash: str = ""
    locations: str = "nbg1,fsn1,hel1"
    server_types: str = "cx23"
    image: str = "ubuntu-24.04"
    dns_zone_id: str = ""
    max_volume_size_gb: int = 100
    max_fleet_servers: int = 5
    enable_backups: bool = True
    allow_ssh: bool = False
    ssh_key_ids: str = ""
    firewall_ids: str = ""
    max_user_data_bytes: int = 32768


def _csv_values(raw: str) -> set[str]:
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorize(authorization: str | None, settings: BrokerSettings) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized()
    credential = authorization[7:].strip()
    if not credential or not verify_secret(credential, settings.mc_token_hash):
        raise _unauthorized()


def _validate_firewall(rules: tuple[FirewallRule, ...], settings: BrokerSettings) -> None:
    expected_ports = {"80", "443"}
    allowed_ports = set(expected_ports)
    if settings.allow_ssh:
        allowed_ports.add("22")
    seen: set[str] = set()
    for rule in rules:
        if (
            rule.direction != "in"
            or rule.protocol != "tcp"
            or rule.port not in allowed_ports
            or tuple(rule.source_ips) != _PUBLIC_SOURCES
            or rule.port in seen
        ):
            raise ValueError("invalid firewall request")
        seen.add(rule.port)
    if not expected_ports.issubset(seen):
        raise ValueError("invalid firewall request")


def validate_provision_request(
    *,
    server,
    volume,
    dns,
    firewall,
    settings: BrokerSettings,
) -> None:
    """Validate broker-owned limits before a provider mutation is possible."""

    deployment_id = str((server.labels or {}).get("deployment_id", ""))
    if not _DEPLOYMENT_ID.fullmatch(deployment_id):
        raise ValueError("invalid provision request")
    if server.labels != {"deployment_id": deployment_id, FLEET_LABEL_KEY: FLEET_LABEL_VALUE}:
        raise ValueError("invalid provision request")
    if (
        not _HOST_LABEL.fullmatch(server.name)
        or not server.name.startswith("onebrain-")
        or server.location not in _csv_values(settings.locations)
        or server.server_type not in _csv_values(settings.server_types)
        or server.image != settings.image
        or len(server.user_data.encode("utf-8")) > settings.max_user_data_bytes
        or server.volume_ids
    ):
        raise ValueError("invalid provision request")

    allowed_ssh_keys = _csv_values(settings.ssh_key_ids)
    requested_ssh_keys = {str(value) for value in server.ssh_key_ids}
    if requested_ssh_keys and (not settings.allow_ssh or not requested_ssh_keys.issubset(allowed_ssh_keys)):
        raise ValueError("invalid provision request")

    allowed_firewall_ids = _csv_values(settings.firewall_ids)
    requested_firewall_ids = {str(value) for value in server.firewall_ids}
    if requested_firewall_ids and not requested_firewall_ids.issubset(allowed_firewall_ids):
        raise ValueError("invalid provision request")
    if firewall is not None:
        if requested_firewall_ids or firewall.name != f"{server.name}-fw" or firewall.labels != {"deployment_id": deployment_id}:
            raise ValueError("invalid provision request")
        _validate_firewall(firewall.rules, settings)

    if volume is not None:
        if (
            volume.name != f"{server.name}-data"
            or volume.location != server.location
            or volume.labels != {"deployment_id": deployment_id}
            or volume.size_gb < 1
            or volume.size_gb > settings.max_volume_size_gb
        ):
            raise ValueError("invalid provision request")

    host_label = server.name.removeprefix("onebrain-")
    if dns is not None:
        if (
            not settings.dns_zone_id
            or dns.zone_id != settings.dns_zone_id
            or dns.name != host_label
            or dns.ipv4 != ""
            or dns.ttl != 300
        ):
            raise ValueError("invalid provision request")


def create_broker_app(*, settings: BrokerSettings | None = None, client=None) -> FastAPI:
    """Build the minimal broker application. Tests inject a fake cloud client."""

    settings = settings or BrokerSettings()
    if not settings.api_token or not settings.mc_token_hash:
        raise RuntimeError("HETZNER_BROKER_API_TOKEN and HETZNER_BROKER_MC_TOKEN_HASH are required")
    if settings.max_volume_size_gb < 1 or settings.max_fleet_servers < 1 or settings.max_user_data_bytes < 1:
        raise RuntimeError("Hetzner broker limits must be positive")
    cloud_client = client or UrllibHetznerClient(settings.api_token)
    broker = InProcessHetznerBroker(
        cloud_client,
        max_fleet_servers=settings.max_fleet_servers,
        enable_backups=settings.enable_backups,
    )
    app = FastAPI(title="onebrain-hetzner-broker", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/provision")
    async def provision(request: Request, authorization: str | None = Header(default=None)):
        _authorize(authorization, settings)
        content_length = request.headers.get("content-length")
        max_request_bytes = settings.max_user_data_bytes + 16 * 1024
        if content_length and content_length.isdigit() and int(content_length) > max_request_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
        raw = await request.body()
        if len(raw) > max_request_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
        try:
            server, volume, dns, firewall = decode_provision_request(json.loads(raw.decode("utf-8")))
            validate_provision_request(
                server=server, volume=volume, dns=dns, firewall=firewall, settings=settings
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid provision request")
        try:
            result = broker.provision_box(server=server, volume=volume, dns=dns, firewall=firewall)
        except Exception as exc:
            # No request body, provider body, or credential is ever written to a log.
            logger.warning("Hetzner broker provisioning failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Provisioning failed")
        return encode_provision_result(result)

    return app

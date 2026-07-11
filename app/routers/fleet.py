"""Mission Control fleet surface.

- POST /api/fleet/heartbeat : a deployment reports metadata-only status. Bearer
  fleet-key auth; the key is pinned to a deployment and can only report for it.
- Key management + overview: operator-admin only.

Registered only when ONEBRAIN_OPERATOR_MODE=true (Mission Control), so a
customer-serving deployment never ingests or exposes fleet state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth.principal import Principal, resolve_principal
from app.deps import get_control_plane_store, get_fleet_store
from app.fleet.base import FleetKey, Heartbeat
from app.fleet.heartbeat import FleetHeartbeat
from app.fleet.keys import generate_fleet_key, hash_secret, parse_fleet_key, verify_secret

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_operator_admin(principal: Principal) -> None:
    # Operator-admin == an admin of the Mission Control deployment itself. In
    # operator mode there are no customer accounts, so this is the operator.
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Operator admin required.")


# --- heartbeat ingest ---------------------------------------------------------

class HeartbeatAck(BaseModel):
    received: bool = True
    deployment_id: str
    # Desired-state config channel (Phase 3). Empty for now; the contract is here
    # so a reporter can pull-and-ack config without a client change later.
    config: dict = Field(default_factory=dict)


def _authenticate_fleet_key(authorization: str, expected_deployment_id: str):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    parsed = parse_fleet_key(token) if token else None
    if not parsed:
        raise HTTPException(status_code=401, detail="Missing or malformed fleet key.")
    key_id, secret = parsed
    key = get_fleet_store().get_key(key_id)
    if not key or key.status != "active" or not verify_secret(secret, key.key_hash):
        raise HTTPException(status_code=401, detail="Invalid fleet key.")
    # A key may only report for its own deployment.
    if key.deployment_id != expected_deployment_id:
        raise HTTPException(status_code=403, detail="This fleet key cannot report for that deployment.")
    return key


@router.post("/heartbeat", response_model=HeartbeatAck)
def ingest_heartbeat(body: FleetHeartbeat, authorization: str = Header(default="")):
    key = _authenticate_fleet_key(authorization, body.deployment_id)
    store = get_fleet_store()
    store.touch_key(key.id, _now())
    store.record_heartbeat(Heartbeat(
        id=f"hb_{uuid4().hex}",
        deployment_id=body.deployment_id,
        contract_version=body.contract_version,
        reported_at=body.reported_at,
        received_at=_now(),
        healthy=body.healthy,
        version=body.onebrain.version,
        migration_revision=body.onebrain.migration_revision,
        payload=body.model_dump(),
    ))
    return HeartbeatAck(deployment_id=body.deployment_id)


# --- key management (operator-admin) -----------------------------------------

class FleetKeyCreate(BaseModel):
    deployment_id: str = Field(min_length=1, max_length=120)
    label: str = Field(default="", max_length=200)


class MintedFleetKey(BaseModel):
    id: str
    deployment_id: str
    label: str = ""
    token: str  # shown once, never again


class FleetKeyInfo(BaseModel):
    id: str
    deployment_id: str
    label: str = ""
    status: str
    created_at: str = ""
    last_used_at: str = ""


def _key_info(key: FleetKey) -> FleetKeyInfo:
    return FleetKeyInfo(id=key.id, deployment_id=key.deployment_id, label=key.label,
                        status=key.status, created_at=key.created_at, last_used_at=key.last_used_at)


@router.post("/keys", response_model=MintedFleetKey)
def mint_fleet_key(body: FleetKeyCreate, principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    if not get_control_plane_store().get_deployment(body.deployment_id):
        raise HTTPException(status_code=404, detail="No such deployment in the registry.")
    key_id, secret, token = generate_fleet_key()
    get_fleet_store().create_key(FleetKey(
        id=key_id, key_hash=hash_secret(secret), deployment_id=body.deployment_id,
        label=body.label.strip(), created_at=_now(),
    ))
    return MintedFleetKey(id=key_id, deployment_id=body.deployment_id, label=body.label.strip(), token=token)


@router.get("/keys", response_model=list[FleetKeyInfo])
def list_fleet_keys(deployment_id: str = "", principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    return [_key_info(k) for k in get_fleet_store().list_keys(deployment_id)]


@router.post("/keys/{key_id}/revoke")
def revoke_fleet_key(key_id: str, principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    if not get_fleet_store().revoke_key(key_id):
        raise HTTPException(status_code=404, detail="No active fleet key with that id.")
    return {"revoked": key_id}


# --- fleet overview (operator-admin) -----------------------------------------

class DeploymentOverview(BaseModel):
    deployment_id: str
    customer_name: str
    environment: str
    deployment_type: str
    release_ring: str
    status: str
    current_version: str = ""
    healthy: bool | None = None
    reported_version: str = ""
    migration_revision: str = ""
    last_reported_at: str = ""
    last_received_at: str = ""
    counts: dict = Field(default_factory=dict)
    open_alerts: list[str] = Field(default_factory=list)


class FleetOverviewOut(BaseModel):
    generated_at: str
    deployments: list[DeploymentOverview]
    total: int
    healthy: int
    with_open_alerts: int


@router.get("/overview", response_model=FleetOverviewOut)
def fleet_overview(principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    control = get_control_plane_store()
    fleet = get_fleet_store()
    latest = fleet.latest_heartbeats()

    rows: list[DeploymentOverview] = []
    healthy = 0
    with_alerts = 0
    for dep in control.list_deployments():
        hb = latest.get(dep.id)
        alerts = [a.kind for a in fleet.list_open_alerts(dep.id)]
        ob = (hb.payload.get("onebrain") if hb else None) or {}
        row = DeploymentOverview(
            deployment_id=dep.id,
            customer_name=dep.customer_name,
            environment=dep.environment,
            deployment_type=dep.deployment_type,
            release_ring=dep.release_ring,
            status=dep.status,
            current_version=dep.current_version,
            healthy=hb.healthy if hb else None,
            reported_version=hb.version if hb else "",
            migration_revision=hb.migration_revision if hb else "",
            last_reported_at=hb.reported_at if hb else "",
            last_received_at=hb.received_at if hb else "",
            counts={
                "users": ob.get("users", 0),
                "accounts": ob.get("accounts", 0),
                "chunks": ob.get("chunks", 0),
                "jobs_pending": ob.get("jobs_pending", 0),
                "jobs_failed": ob.get("jobs_failed", 0),
            } if hb else {},
            open_alerts=alerts,
        )
        rows.append(row)
        if hb and hb.healthy:
            healthy += 1
        if alerts:
            with_alerts += 1

    return FleetOverviewOut(
        generated_at=_now(),
        deployments=rows,
        total=len(rows),
        healthy=healthy,
        with_open_alerts=with_alerts,
    )

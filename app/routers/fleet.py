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
from app.config import get_settings
from app.controlplane.desired_state import active_pull_attempt_id, sign_desired_state_for
from app.deps import get_control_plane_store, get_fleet_heartbeat_rate_limiter, get_fleet_store
from app.fleet.base import FleetKey, Heartbeat
from app.fleet.enrollment import fleet_enrollment_vars, mint_deployment_fleet_key
from app.fleet.heartbeat import AnyFleetHeartbeat, FleetHeartbeat  # noqa: F401 — FleetHeartbeat kept for typing
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
    # Desired-state channel. Empty today. Phase-4 pull orchestration fills:
    #   config["desired_state"]  = app.trust.envelope.DesiredStateEnvelope.model_dump()
    #                              ADVISORY FAST-PATH ONLY (B8): the AUTHORITATIVE
    #                              channel is the host update.sh's direct GET from
    #                              MC with its own read-scoped token (P1-F) — an
    #                              ack-delivered envelope shares the app's failure
    #                              domain (a bricked app container stops
    #                              heartbeating and would sever its own recovery
    #                              path). The box-side verifier
    #                              (app.trust.envelope.verify_desired_state) is
    #                              identical for both channels; the box never
    #                              trusts MC's word either way.
    #   config["secrets_epoch"]  = int (bumps when the box should re-fetch secrets
    #                              via the bootstrap-token exchange channel, P1-E)
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


def _reject_skewed_reported_at(reported_at: str) -> None:
    """received_at (server clock) stays authoritative for the watchdog, but a
    reported_at implausibly far from now signals a replayed or forged heartbeat."""
    max_skew = get_settings().fleet_heartbeat_max_skew_seconds
    if max_skew <= 0:
        return
    try:
        reported = datetime.fromisoformat(reported_at)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed reported_at.")
    if reported.tzinfo is None:
        reported = reported.replace(tzinfo=timezone.utc)
    if abs((datetime.now(timezone.utc) - reported).total_seconds()) > max_skew:
        raise HTTPException(status_code=400, detail="reported_at is too far from server time.")


@router.post("/heartbeat", response_model=HeartbeatAck)
def ingest_heartbeat(body: AnyFleetHeartbeat, authorization: str = Header(default="")):
    key = _authenticate_fleet_key(authorization, body.deployment_id)
    # Cap per-deployment posting rate so a leaked/misused key can't flood the
    # append-only heartbeat table; reject implausibly-skewed reported_at.
    wait = get_fleet_heartbeat_rate_limiter().check(f"hb:{body.deployment_id}")
    if wait:
        raise HTTPException(status_code=429, detail="Heartbeat rate limit exceeded.",
                            headers={"Retry-After": str(wait)})
    _reject_skewed_reported_at(body.reported_at)
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
    # ADVISORY fast-path only (B8): the AUTHORITATIVE channel is the box's own GET
    # /desired-state below. Behaviour-inert while emission is off — env is None when
    # fleet_desired_state_private_key is unset, so config stays {} byte-for-byte with
    # today's ack (pinned by test_heartbeat_ack_is_inert_when_emission_off).
    ack = HeartbeatAck(deployment_id=body.deployment_id)
    control = get_control_plane_store()
    env = sign_desired_state_for(control, body.deployment_id, settings=get_settings(),
                                 now=datetime.now(timezone.utc))
    if env is not None:
        ack.config["desired_state"] = {
            "envelope": env.model_dump(),
            "attempt_id": active_pull_attempt_id(control, body.deployment_id),
        }
    return ack


@router.get("/desired-state", response_model=dict | None)
def get_desired_state(authorization: str = Header(default=""),
                      x_onebrain_deployment_id: str = Header(default="")):
    """The AUTHORITATIVE desired-state channel (P1-F): the box authenticates with its
    OWN fleet key (the same _authenticate_fleet_key as heartbeat, pinned to a
    deployment) and can fetch ONLY its own desired-state. Read-only; no side effects.
    Returns None while emission is dormant (no wrapper key configured). The response
    wraps the signed, verify-don't-trust envelope alongside the unsigned out-of-band
    attempt_id hint — exactly the shape the box verifier (P4-08) consumes."""
    key = _authenticate_fleet_key(authorization, x_onebrain_deployment_id.strip())
    control = get_control_plane_store()
    env = sign_desired_state_for(control, key.deployment_id, settings=get_settings(),
                                 now=datetime.now(timezone.utc))
    if env is None:
        return None
    return {"envelope": env.model_dump(), "attempt_id": active_pull_attempt_id(control, key.deployment_id)}


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


# --- enrollment (operator-admin) ---------------------------------------------

class EnrollmentOut(BaseModel):
    deployment_id: str
    key_id: str
    env: dict  # ONEBRAIN_FLEET_URL / ONEBRAIN_DEPLOYMENT_ID / ONEBRAIN_FLEET_KEY (token shown once)


@router.post("/deployments/{deployment_id}/enroll", response_model=EnrollmentOut)
def enroll_deployment(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    """Mint a fleet key for a deployment and return the three env vars its reporter
    needs. The operator applies these to the deployment's Railway env (provisioning
    does this automatically for new deployments)."""
    _require_operator_admin(principal)
    if not get_control_plane_store().get_deployment(deployment_id):
        raise HTTPException(status_code=404, detail="No such deployment in the registry.")
    settings = get_settings()
    if not settings.fleet_public_url:
        raise HTTPException(status_code=409, detail="ONEBRAIN_FLEET_PUBLIC_URL is not configured on Mission Control.")
    fleet_store = get_fleet_store()
    # Rotate: re-enrolling supersedes the deployment's prior keys so active keys
    # don't accumulate unbounded (each stays valid for heartbeat ingest otherwise).
    for key in fleet_store.list_keys(deployment_id):
        if key.status == "active":
            fleet_store.revoke_key(key.id)
    key_id, token = mint_deployment_fleet_key(fleet_store, deployment_id,
                                              label=f"enrollment:{deployment_id}", now_iso=_now())
    return EnrollmentOut(
        deployment_id=deployment_id, key_id=key_id,
        env=fleet_enrollment_vars(settings.fleet_public_url, deployment_id, token),
    )


# --- heartbeat history / analytics (operator-admin) --------------------------

class HeartbeatPoint(BaseModel):
    received_at: str
    reported_at: str = ""
    healthy: bool
    version: str = ""
    counts: dict = Field(default_factory=dict)


class HeartbeatHistoryOut(BaseModel):
    deployment_id: str
    points: list[HeartbeatPoint]
    total: int


@router.get("/deployments/{deployment_id}/history", response_model=HeartbeatHistoryOut)
def heartbeat_history(deployment_id: str, since: str = "", limit: int = 500,
                      principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    since = since.strip()
    if since:
        try:
            datetime.fromisoformat(since)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Malformed 'since' timestamp (use ISO-8601).")
    rows = get_fleet_store().list_heartbeats(deployment_id, since_iso=since, limit=max(1, min(limit, 5000)))
    points = []
    for hb in rows:
        ob = (hb.payload.get("onebrain") if hb.payload else None) or {}
        points.append(HeartbeatPoint(
            received_at=hb.received_at, reported_at=hb.reported_at, healthy=hb.healthy, version=hb.version,
            counts={k: ob.get(k, 0) for k in
                    ("users", "accounts", "chunks", "intake_records", "active_service_keys",
                     "jobs_pending", "jobs_failed", "auth_failures_recent", "api_5xx_recent")},
        ))
    return HeartbeatHistoryOut(deployment_id=deployment_id, points=points, total=len(points))

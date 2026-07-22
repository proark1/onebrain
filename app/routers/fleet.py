"""Mission Control fleet surface.

- POST /api/fleet/heartbeat : a deployment reports metadata-only status. Bearer
  fleet-key auth; the key is pinned to a deployment and can only report for it.
- Key management + overview: operator-admin only.

Registered only when ONEBRAIN_OPERATOR_MODE=true (Mission Control), so a
customer-serving deployment never ingests or exposes fleet state.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.auth.principal import Principal, resolve_principal
from app.config import get_settings
from app.controlplane.base import ServedFloorBump
from app.controlplane.promotion import reconcile_heartbeat_promotion
from app.controlplane.desired_state import (
    active_pull_attempt_id,
    active_signer_in_served_set,
    sign_desired_state_for,
)
from app.deps import (
    get_control_plane_store,
    get_fleet_bootstrap_rate_limiter,
    get_fleet_heartbeat_rate_limiter,
    get_fleet_store,
    get_provisioning_run_store,
)
from app.fleet.base import FleetKey, Heartbeat
from app.fleet.bootstrap_bundle import backfill_runtime_db_passwords, render_dotenv
from app.fleet.enrollment import fleet_enrollment_vars, mint_deployment_fleet_key
from app.fleet.heartbeat import AnyFleetHeartbeat, FleetHeartbeat, StorageReport  # noqa: F401 — FleetHeartbeat kept for typing
from app.fleet.keys import (
    generate_fleet_key,
    hash_secret,
    parse_bootstrap_token,
    parse_fleet_key,
    verify_secret,
)
from app.provisioning.runs import OneTimeSecretCipher
from app.trust.envelope import FloorBump, verify_floor_bump

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
    wait = get_fleet_heartbeat_rate_limiter().check(body.deployment_id)
    if wait:
        raise HTTPException(status_code=429, detail="Heartbeat rate limit exceeded.",
                            headers={"Retry-After": str(wait)})
    _reject_skewed_reported_at(body.reported_at)
    store = get_fleet_store()
    received_at = _now()
    store.touch_key(key.id, received_at)
    store.record_heartbeat(Heartbeat(
        id=f"hb_{uuid4().hex}",
        deployment_id=body.deployment_id,
        contract_version=body.contract_version,
        reported_at=body.reported_at,
        received_at=received_at,
        healthy=body.healthy,
        version=body.onebrain.version,
        migration_revision=body.onebrain.migration_revision,
        payload=body.model_dump(),
    ))
    control = get_control_plane_store()
    reconcile_heartbeat_promotion(control, body, received_at=received_at)
    deployment = control.get_deployment(body.deployment_id)
    if deployment and deployment.is_release_gate and body.healthy:
        # A candidate may have arrived before the gate was healthy. The first
        # healthy heartbeat opens exactly one waiting candidate; later candidates
        # remain pending behind the active dev rollout.
        from app.routers.operator import dispatch_waiting_development_candidate

        dispatch_waiting_development_candidate(control, actor=f"fleet:{body.deployment_id}")
    # ADVISORY fast-path only (B8): the AUTHORITATIVE channel is the box's own GET
    # /desired-state below. Behaviour-inert while emission is off — env is None when
    # fleet_desired_state_private_key is unset, so config stays {} byte-for-byte with
    # today's ack (pinned by test_heartbeat_ack_is_inert_when_emission_off).
    ack = HeartbeatAck(deployment_id=body.deployment_id)
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


# --- bootstrap-token secret exchange (P5-03) ---------------------------------
# A box exchanges its single-use FIRST-BOOT token (or, on a rotation tick, its fleet
# key) for its re-readable secret bundle, delivered as an /opt/onebrain/.env body. The
# token is consumed atomically only as the LAST step of a successful 200 AFTER the
# bundle is assembled (G1-2/G1-8), so a lost response never bricks the box.

class BootstrapExchangeOut(BaseModel):
    secrets_epoch: int
    dotenv: str  # the /opt/onebrain/.env body — NEVER logged (G1-5)


def _bootstrap_token_unexpired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


@router.post("/bootstrap", response_model=BootstrapExchangeOut)
def bootstrap_exchange(authorization: str = Header(default=""),
                       x_onebrain_deployment_id: str = Header(default="")):
    """Deliver a box's re-readable secret bundle as an /opt/onebrain/.env body. Auth is
    EITHER a single-use, unconsumed+unexpired bootstrap token (first boot) OR the
    deployment's fleet key (rotation re-fetch); the deployment is derived from the
    authenticated credential — NEVER a free parameter — so a box can only fetch its OWN
    bundle. Ordering (G1-2): validate the token, load+decrypt the bundle, assemble the
    body, and consume the token ONLY as the last step before returning 200."""
    settings = get_settings()
    prov = get_provisioning_run_store()
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""

    token_secret = ""
    if token.startswith("bt_"):
        parsed = parse_bootstrap_token(token)
        if not parsed:
            raise HTTPException(status_code=401, detail="Malformed bootstrap token.")
        token_secret = parsed[1]
        record = prov.get_bootstrap_token(hash_secret(token_secret))
        if record is None or record.consumed_at or not _bootstrap_token_unexpired(record.expires_at):
            raise HTTPException(status_code=401, detail="Invalid, expired, or consumed bootstrap token.")
        deployment_id = record.deployment_id
    else:
        # Rotation path: a valid fleet key pinned to the header deployment (G1-5 — a
        # box with a bundle may now fetch its OWN bundle with its fleet key).
        key = _authenticate_fleet_key(authorization, x_onebrain_deployment_id.strip())
        deployment_id = key.deployment_id

    # G1-5: a DEDICATED, aggressively-low rate limit keyed by the resolved deployment —
    # a single fetch exfiltrates the whole bundle, so a leaked key cannot poll it.
    wait = get_fleet_bootstrap_rate_limiter().check(deployment_id)
    if wait:
        raise HTTPException(status_code=429, detail="Bootstrap rate limit exceeded.",
                            headers={"Retry-After": str(wait)})

    bundle_row = prov.get_secret_bundle(deployment_id)
    if bundle_row is None:
        raise HTTPException(status_code=404, detail="No secret bundle for that deployment.")
    try:
        bundle = json.loads(OneTimeSecretCipher(settings).open_bundle(bundle_row.ciphertext))
    except ValueError:
        # A decrypt failure must NOT consume the token (G1-2) — the box holds + retries.
        raise HTTPException(status_code=500, detail="Secret bundle could not be opened.")

    # Overlay the CURRENT accepted wrapper-key set so a rotation is reflected WITHOUT
    # re-encrypting every bundle; then run the G1-1 interlock on that overlaid set —
    # never hand a box a set that excludes MC's active signer.
    bundle["UPDATE_DESIRED_STATE_PUBLIC_KEYS"] = (
        settings.fleet_desired_state_public_keys or settings.fleet_desired_state_public_key)
    if not active_signer_in_served_set(settings):
        raise HTTPException(status_code=409, detail="active_signer_not_in_public_key_set")
    body = BootstrapExchangeOut(secrets_epoch=bundle_row.secrets_epoch, dotenv=render_dotenv(bundle))

    # G1-2/G1-8: consume the one-time token atomically as the LAST step, after the body is
    # assembled — so a decrypt/assembly failure never burns the token (the box holds + retries).
    # A NULL return means a concurrent request already won the race -> 401 with no body. The
    # fleet-key rotation path consumes nothing.
    #
    # ACCEPTED RESIDUAL (low): the DB consume commits just BEFORE this 200 reaches the box, so
    # a response lost in transit (TCP reset / LB timeout mid-body) still burns the token — the
    # box finds no /opt/onebrain/.env, re-presents the same (now-consumed) token next tick, and
    # 401s. This is inherent to single-use semantics (an ACK-gated consume only trades it for a
    # replay window). A first-boot box holds NO data, so the cheap, correct mitigation is
    # RE-PROVISION: the stored bundle is re-readable and _provision_box_secrets mints a FRESH
    # usable token on every re-dispatch. The stall is operator-visible (the first-boot smoke
    # callback fails). Do NOT read this "consume-last" ordering as "a lost response never bricks".
    if token_secret:
        if prov.consume_bootstrap_token(hash_secret(token_secret)) is None:
            raise HTTPException(status_code=401, detail="Bootstrap token already consumed.")
    return body


# --- floor-bump serving (P5-01 revocation kill-switch) -----------------------
# The offline-signed floor bump is the revocation mechanism for a yanked-but-still-
# signed release. The release key is NEVER on MC: the operator signs the bump offline
# (scripts/sign_release.py bump-floor) and uploads the finished JSON; MC only stores
# and serves it verbatim. The box re-verifies the OFFLINE signature itself, so a
# compromised MC serving a forged bump is rejected box-side; MC's store-time
# verification is defense-in-depth + operator UX.

class FloorBumpServeOut(BaseModel):
    floor_bump: dict  # the pre-signed FloorBump.model_dump() — served verbatim


class FloorBumpSet(BaseModel):
    bump: dict  # a signed FloorBump JSON (from scripts/sign_release.py bump-floor)


class FloorBumpInfo(BaseModel):
    scope: str
    floor_version: str = ""
    updated_by: str = ""
    updated_at: str = ""


@router.get("/floor-bump", response_model=FloorBumpServeOut | None)
def get_floor_bump(authorization: str = Header(default=""),
                   x_onebrain_deployment_id: str = Header(default="")):
    """Serve the most-specific pre-signed floor bump for the authenticated box.
    Fleet-key auth (same _authenticate_fleet_key as heartbeat/desired-state, pinned
    to a deployment); read-only, no side effects. Resolves the deployment-scoped bump
    first, then the fleet-wide '*' fallback. Returns None when neither exists. MC does
    NOT re-sign or mutate the bump."""
    key = _authenticate_fleet_key(authorization, x_onebrain_deployment_id.strip())
    control = get_control_plane_store()
    served = control.get_served_floor_bump(key.deployment_id) or control.get_served_floor_bump("*")
    if served is None:
        return None
    return FloorBumpServeOut(floor_bump=json.loads(served.bump_json))


@router.post("/floor-bump", response_model=FloorBumpInfo)
def set_floor_bump(body: FloorBumpSet, principal: Principal = Depends(resolve_principal)):
    """Set the served floor bump (operator-admin). MC verifies the OFFLINE signature
    before storing. 409 when release_verify_public_key is unset (cannot verify an
    unverifiable kill-switch). 400 on a malformed body or a rejected signature.
    (G2-4) deployment_scope is fed as expected_deployment_id, so scope_mismatch is
    UNREACHABLE here — the only reachable store-time reject codes are
    signature_invalid and version_not_comparable. Real per-box scope enforcement
    happens box-side against the box's OWN deployment_id."""
    _require_operator_admin(principal)
    settings = get_settings()
    if not settings.release_verify_public_key:
        raise HTTPException(status_code=409,
                            detail="ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY is not configured; refusing to serve an unverifiable floor bump.")
    try:
        bump = FloorBump.model_validate(body.bump)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Malformed floor bump.")
    errors = verify_floor_bump(bump, release_public_key_b64=settings.release_verify_public_key,
                               expected_deployment_id=bump.deployment_scope)
    if errors:
        raise HTTPException(status_code=400, detail=f"Floor bump rejected: {errors[0]}")
    stored = get_control_plane_store().set_served_floor_bump(ServedFloorBump(
        scope=bump.deployment_scope,
        bump_json=bump.model_dump_json(),
        floor_version=bump.floor_version,
        updated_by=principal.user_id,
    ))
    return FloorBumpInfo(scope=stored.scope, floor_version=stored.floor_version,
                         updated_by=stored.updated_by, updated_at=stored.updated_at)


@router.delete("/floor-bump")
def clear_floor_bump(scope: str = "", principal: Principal = Depends(resolve_principal)):
    """Clear the served bump for a scope ('*' or a deployment_id); 404 if absent."""
    _require_operator_admin(principal)
    if not get_control_plane_store().clear_served_floor_bump(scope):
        raise HTTPException(status_code=404, detail="No served floor bump for that scope.")
    return {"cleared": scope}


@router.get("/floor-bumps", response_model=list[FloorBumpInfo])
def list_floor_bumps(principal: Principal = Depends(resolve_principal)):
    """List the currently-served bumps (operator visibility of the kill-switch state)."""
    _require_operator_admin(principal)
    return [FloorBumpInfo(scope=b.scope, floor_version=b.floor_version,
                          updated_by=b.updated_by, updated_at=b.updated_at)
            for b in get_control_plane_store().list_served_floor_bumps()]


# --- desired-state wrapper-key rotation (P5-02, operator-admin) ---------------
# MC signs desired-state with the ONE online private key; boxes accept ANY key in a
# delivered SET. Rotation bumps each box's secrets_epoch so it re-fetches the new
# pubkey set (via the P5-03 bundle channel). No key material passes through these
# endpoints — the keys are config, delivered by the bundle. G1-1 interlock runs
# FIRST: refuse to bump an epoch while MC is signing with a key that is not in the
# served set (that would strand the fleet at envelope_signature_invalid).

class RotateResult(BaseModel):
    rotated: int          # deployments whose secrets_epoch was bumped


class DeploymentRotateResult(BaseModel):
    deployment_id: str
    secrets_epoch: int


def _require_active_signer_in_served_set() -> None:
    if not active_signer_in_served_set(get_settings()):
        raise HTTPException(status_code=409, detail="active_signer_not_in_public_key_set")


@router.post("/rotate-desired-state-key", response_model=RotateResult)
def rotate_desired_state_key(principal: Principal = Depends(resolve_principal)):
    """Tell every provisioned box to re-fetch the accepted wrapper-key set: bump the
    secrets_epoch of each deployment that has a secret bundle. Idempotent-safe
    (re-bumping just raises the epoch). Refuses (409) when the active signer is not
    in the served set (G1-1)."""
    _require_operator_admin(principal)
    _require_active_signer_in_served_set()
    control = get_control_plane_store()
    prov = get_provisioning_run_store()
    rotated = 0
    for dep in control.list_deployments():
        # Only boxes with a bundle re-fetch have a local state file — skip the rest.
        # those rather than raising, so the count reflects the pull-managed fleet.
        if prov.get_secret_bundle(dep.id) is not None:
            prov.bump_secrets_epoch(dep.id)
            rotated += 1
    return RotateResult(rotated=rotated)


@router.post("/deployments/{deployment_id}/rotate-secrets", response_model=DeploymentRotateResult)
def rotate_deployment_secrets(deployment_id: str, principal: Principal = Depends(resolve_principal)):
    """Bump a single deployment's secrets_epoch (single-box secret rotation, e.g. a
    DB-password re-mint via P5-03). 404 if the deployment is unknown or has no bundle.
    Same G1-1 interlock first."""
    _require_operator_admin(principal)
    _require_active_signer_in_served_set()
    if not get_control_plane_store().get_deployment(deployment_id):
        raise HTTPException(status_code=404, detail="No such deployment in the registry.")
    prov = get_provisioning_run_store()
    if prov.get_secret_bundle(deployment_id) is None:
        raise HTTPException(status_code=404, detail="No secret bundle for that deployment.")
    return DeploymentRotateResult(deployment_id=deployment_id, secrets_epoch=prov.bump_secrets_epoch(deployment_id))


class RuntimeDbCredentialBackfillResult(BaseModel):
    deployment_id: str
    updated: bool
    secrets_epoch: int


@router.post(
    "/deployments/{deployment_id}/backfill-runtime-db-credentials",
    response_model=RuntimeDbCredentialBackfillResult,
)
def backfill_runtime_db_credentials(
    deployment_id: str,
    principal: Principal = Depends(resolve_principal),
):
    """Add missing legacy runtime secrets to a legacy box bundle.

    This is an operator-admin-only, MC-side migration path for bundles sealed
    before the full restricted-runtime role split and login-rate-limit secret
    existed. It decrypts only in MC memory, adds *only* missing values (including
    a 32+ character login rate-limit secret), re-seals before storage, and returns
    no secret material. A successful change then bumps secrets_epoch so the box
    re-fetches its authoritative bundle. Never generate these values on a box:
    that would leave MC unable to service a later rotation. The route keeps its
    original DB-credential name for compatibility.
    """

    _require_operator_admin(principal)
    _require_active_signer_in_served_set()
    if not get_control_plane_store().get_deployment(deployment_id):
        raise HTTPException(status_code=404, detail="No such deployment in the registry.")
    prov = get_provisioning_run_store()
    bundle_row = prov.get_secret_bundle(deployment_id)
    if bundle_row is None:
        raise HTTPException(status_code=404, detail="No secret bundle for that deployment.")

    try:
        cipher = OneTimeSecretCipher(get_settings())
        decoded = json.loads(cipher.open_bundle(bundle_row.ciphertext))
        if not isinstance(decoded, dict):
            raise ValueError("Secret bundle must be a JSON object.")
        updated_bundle, added = backfill_runtime_db_passwords(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Never return a ciphertext/decryption detail: it can become an oracle
        # for the MC's escrow key or stored secret format.
        raise HTTPException(status_code=500, detail="Secret bundle could not be opened.")

    if not added:
        return RuntimeDbCredentialBackfillResult(
            deployment_id=deployment_id,
            updated=False,
            secrets_epoch=bundle_row.secrets_epoch,
        )

    # upsert_secret_bundle preserves the current epoch; only the explicit bump
    # below signals the host agent to re-fetch this newly sealed content.
    prov.upsert_secret_bundle(replace(
        bundle_row,
        ciphertext=cipher.seal_bundle(json.dumps(updated_bundle, separators=(",", ":"), sort_keys=True)),
    ))
    return RuntimeDbCredentialBackfillResult(
        deployment_id=deployment_id,
        updated=True,
        secrets_epoch=prov.bump_secrets_epoch(deployment_id),
    )


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
    created_at: str = ""
    current_version_deployed_at: str = ""
    is_release_gate: bool = False
    healthy: bool | None = None
    reported_version: str = ""
    migration_revision: str = ""
    last_reported_at: str = ""
    last_received_at: str = ""
    applied_secrets_epoch: int = 0   # G1-3: the epoch the box last applied (rotation convergence)
    counts: dict = Field(default_factory=dict)
    storage: StorageReport = Field(default_factory=StorageReport)
    open_alerts: list[str] = Field(default_factory=list)
    user_management_v1: bool = False
    login_url: str = ""  # https://<box> (or http://<ip>) from the latest provisioning run; "" when unknown


class FleetOverviewOut(BaseModel):
    generated_at: str
    deployments: list[DeploymentOverview]
    total: int
    healthy: int
    with_open_alerts: int


def _reported_storage(payload: dict | None) -> StorageReport:
    """Read additive host-capacity telemetry without trusting old stored JSON."""
    raw = (payload or {}).get("storage") if isinstance(payload, dict) else None
    try:
        return StorageReport.model_validate(raw or {})
    except ValidationError:
        return StorageReport()


def _fleet_login_base_domain(settings) -> str:
    """The fleet's public base domain when boxes are served over DNS + HTTPS, else "".

    Mirrors the provisioner's dns_enabled gate
    (app/provisioning/hetzner/provisioner.py): a box gets a real public hostname only
    when the fleet runs the Hetzner DNS provider with a base domain and a zone id. When
    this returns "", boxes serve plain HTTP on the raw IP and there is no usable login
    link (see _box_login_url)."""
    dns_provider = (getattr(settings, "fleet_dns_provider", "") or "").strip().lower()
    base_domain = (getattr(settings, "fleet_base_domain", "") or "").strip().rstrip(".").lower()
    if dns_provider != "hetzner" or not base_domain or not getattr(settings, "fleet_dns_zone_id", ""):
        return ""
    return base_domain


def _box_login_url(deployment_id: str, base_domain: str) -> str:
    """The box's own HTTPS login address, DERIVED from the deployment id and the fleet
    base domain so it always matches the hostname Caddy holds a certificate for.

    We deliberately do NOT read the provisioning run's external_run_url: the box's
    success callback overwrites it with the raw public IPv4
    (deploy/box/onebrain_gate_report.py), and it is a generic run field that may carry an
    arbitrary provider/workflow URL. An IP-only box (base_domain == "") gets no link -- it
    serves plain HTTP on :80, and boxes render ONEBRAIN_COOKIE_SECURE=true, so a session
    cookie cannot survive an http:// origin anyway."""
    if not base_domain:
        return ""
    from app.provisioning.hetzner.provisioner import _provider_hostname_label
    return f"https://{_provider_hostname_label(deployment_id)}.{base_domain}"


@router.get("/overview", response_model=FleetOverviewOut)
def fleet_overview(principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    control = get_control_plane_store()
    fleet = get_fleet_store()
    latest = fleet.latest_heartbeats()
    login_base = _fleet_login_base_domain(get_settings())

    rows: list[DeploymentOverview] = []
    healthy = 0
    with_alerts = 0
    for dep in control.list_deployments():
        hb = latest.get(dep.id)
        alerts = [a.kind for a in fleet.list_open_alerts(dep.id)]
        ob = (hb.payload.get("onebrain") if hb else None) or {}
        upd = (hb.payload.get("update") if hb else None) or {}   # fleet.v2 UpdateReport (absent on v1)
        row = DeploymentOverview(
            deployment_id=dep.id,
            customer_name=dep.customer_name,
            environment=dep.environment,
            deployment_type=dep.deployment_type,
            release_ring=dep.release_ring,
            status=dep.status,
            current_version=dep.current_version,
            created_at=dep.created_at,
            current_version_deployed_at=dep.current_version_deployed_at,
            is_release_gate=dep.is_release_gate,
            healthy=hb.healthy if hb else None,
            reported_version=hb.version if hb else "",
            migration_revision=hb.migration_revision if hb else "",
            last_reported_at=hb.reported_at if hb else "",
            last_received_at=hb.received_at if hb else "",
            applied_secrets_epoch=int(upd.get("applied_secrets_epoch", 0) or 0),
            counts={
                "users": ob.get("users", 0),
                "accounts": ob.get("accounts", 0),
                "chunks": ob.get("chunks", 0),
                "jobs_pending": ob.get("jobs_pending", 0),
                "jobs_failed": ob.get("jobs_failed", 0),
            } if hb else {},
            storage=_reported_storage(hb.payload if hb else None),
            open_alerts=alerts,
            user_management_v1=bool(ob.get("user_management_v1", False)),
            login_url=_box_login_url(dep.id, login_base),
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
    needs. The operator applies these to the deployment's rendered environment (provisioning
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

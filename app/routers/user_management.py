"""Mission Control operator and deployment-agent user-management APIs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth.principal import Principal, resolve_principal
from app.config import get_settings
from app.deps import (
    get_control_plane_store,
    get_fleet_store,
    get_session_store,
    get_user_management_job_store,
)
from app.routers.fleet import _authenticate_fleet_key
from app.user_management.base import SAFE_ERROR_CODES
from app.user_management.manager import MissionControlUserManagement, is_password_action


agent_router = APIRouter(prefix="/api/fleet/user-management", tags=["fleet-user-management"])
operator_router = APIRouter(prefix="/api/operator/user-management", tags=["operator-user-management"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _manager() -> MissionControlUserManagement:
    return MissionControlUserManagement(store=get_user_management_job_store(), settings=get_settings())


def _require_operator_admin(principal: Principal) -> None:
    if principal.role_id != "admin" or not get_settings().operator_mode:
        raise HTTPException(status_code=403, detail="Operator admin required.")


def _require_recent(principal: Principal) -> None:
    _require_operator_admin(principal)
    session = get_session_store().get(principal.session_id)
    try:
        created = datetime.fromisoformat(session.created_at) if session and session.created_at else None
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except ValueError:
        created = None
    if not created or _now() - created > timedelta(minutes=15):
        raise HTTPException(status_code=403, detail="recent_authentication_required")


def _require_deployment(deployment_id: str):
    deployment = get_control_plane_store().get_deployment(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="No such deployment.")
    heartbeat = get_fleet_store().latest_heartbeat(deployment_id)
    onebrain = (heartbeat.payload.get("onebrain") if heartbeat else None) or {}
    if not onebrain.get("user_management_v1", False):
        raise HTTPException(status_code=409, detail="capability_unavailable")
    return deployment


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment_id: str = Field(min_length=1, max_length=120)
    sender_public_key: str = Field(min_length=1, max_length=256)
    nonce: str = Field(min_length=1, max_length=128)
    ciphertext: str = Field(min_length=1, max_length=1_000_000)


class AgentFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment_id: str = Field(min_length=1, max_length=120)
    error_code: str = Field(min_length=1, max_length=80)


@agent_router.get("/jobs/next")
def next_job(
    authorization: str = Header(default=""),
    x_onebrain_deployment_id: str = Header(default=""),
):
    deployment_id = x_onebrain_deployment_id.strip()
    _authenticate_fleet_key(authorization, deployment_id)
    now = _now()
    job = get_user_management_job_store().lease_next(
        deployment_id,
        now_iso=now.isoformat(),
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    return asdict(_manager().command_for(job)) if job else None


@agent_router.post("/jobs/{job_id}/result")
def submit_result(job_id: str, body: AgentResult, authorization: str = Header(default="")):
    _authenticate_fleet_key(authorization, body.deployment_id)
    job = get_user_management_job_store().get(job_id)
    if not job or job.deployment_id != body.deployment_id:
        raise HTTPException(status_code=404, detail="No such user-management job.")
    try:
        saved = _manager().accept_result(job, body.model_dump(exclude={"deployment_id"}))
    except Exception:
        raise HTTPException(status_code=409, detail="Result rejected.")
    return {"acknowledged": True, "status": saved.status}


@agent_router.post("/jobs/{job_id}/failure")
def submit_failure(job_id: str, body: AgentFailure, authorization: str = Header(default="")):
    _authenticate_fleet_key(authorization, body.deployment_id)
    code = body.error_code if body.error_code in SAFE_ERROR_CODES else "internal_failure"
    saved = get_user_management_job_store().fail(
        job_id,
        body.deployment_id,
        error_code=code,
        completed_at=_now().isoformat(),
    )
    if not saved:
        raise HTTPException(status_code=404, detail="No active user-management job.")
    return {"acknowledged": True, "status": saved.status}


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_deleted: bool = False


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    role_id: str = Field(min_length=1, max_length=64)
    location: str = Field(default="", max_length=120)


def _job_out(job) -> dict:
    value = {
        "id": job.id,
        "deployment_id": job.deployment_id,
        "action": job.action,
        "status": job.status,
        "created_at": job.created_at,
        "expires_at": job.expires_at,
        "completed_at": job.completed_at,
        "error_code": job.error_code,
        "result_available": bool(job.result_ciphertext and not job.result_consumed_at),
    }
    if job.status in {"completed", "failed"} and not is_password_action(job.action):
        result = _manager().read_result(job)
        value["result"] = result
    return value


def _create_job(deployment_id: str, action: str, payload: dict, principal: Principal):
    _require_deployment(deployment_id)
    try:
        return _manager().create_job(
            deployment_id=deployment_id,
            action=action,
            payload=payload,
            requested_by=principal.user_id,
        )
    except RuntimeError:
        raise HTTPException(status_code=409, detail="capability_unavailable")


@operator_router.post("/deployments/{deployment_id}/directory", status_code=202)
def refresh_directory(
    deployment_id: str,
    body: DirectoryRequest,
    principal: Principal = Depends(resolve_principal),
):
    _require_operator_admin(principal)
    return _job_out(_create_job(deployment_id, "directory.snapshot", body.model_dump(), principal))


@operator_router.post("/deployments/{deployment_id}/users", status_code=202)
def create_user(
    deployment_id: str,
    body: CreateUserRequest,
    principal: Principal = Depends(resolve_principal),
):
    _require_recent(principal)
    return _job_out(_create_job(deployment_id, "user.create", body.model_dump(), principal))


def _user_action(deployment_id: str, user_id: str, action: str, principal: Principal):
    _require_recent(principal)
    return _job_out(_create_job(deployment_id, action, {"user_id": user_id}, principal))


@operator_router.post("/deployments/{deployment_id}/users/{user_id}/reset-password", status_code=202)
def reset_password(deployment_id: str, user_id: str, principal: Principal = Depends(resolve_principal)):
    return _user_action(deployment_id, user_id, "user.password.reset", principal)


@operator_router.post("/deployments/{deployment_id}/users/{user_id}/disable", status_code=202)
def disable_user(deployment_id: str, user_id: str, principal: Principal = Depends(resolve_principal)):
    return _user_action(deployment_id, user_id, "user.disable", principal)


@operator_router.post("/deployments/{deployment_id}/users/{user_id}/enable", status_code=202)
def enable_user(deployment_id: str, user_id: str, principal: Principal = Depends(resolve_principal)):
    return _user_action(deployment_id, user_id, "user.enable", principal)


@operator_router.delete("/deployments/{deployment_id}/users/{user_id}", status_code=202)
def delete_user(deployment_id: str, user_id: str, principal: Principal = Depends(resolve_principal)):
    return _user_action(deployment_id, user_id, "user.delete", principal)


@operator_router.get("/jobs/{job_id}")
def get_job(job_id: str, principal: Principal = Depends(resolve_principal)):
    _require_operator_admin(principal)
    job = get_user_management_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such user-management job.")
    return _job_out(job)


@operator_router.post("/jobs/{job_id}/reveal")
def reveal_secret(job_id: str, principal: Principal = Depends(resolve_principal)):
    _require_recent(principal)
    job = get_user_management_job_store().get(job_id)
    if not job or not is_password_action(job.action):
        raise HTTPException(status_code=404, detail="No revealable secret for that job.")
    result = _manager().consume_secret_result(job_id)
    if not result:
        raise HTTPException(status_code=410, detail="secret_already_consumed")
    return result

"""Rollout execution callback surface.

The update-customer workflow posts lifecycle callbacks here (running / succeeded /
failed), driving a rollout to completion. Authentication reuses the provisioning
callback scheme verbatim (bearer + X-OneBrain-Callback-Key-Id, HMAC-hashed shared
secret). Registered only on an operator surface, like provisioning.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.controlplane.fleet_runner import advance_fleet_on_child
from app.controlplane.promotion import reconcile_rollout_promotion
from app.controlplane.rollout_exec import TERMINAL_EXEC_STATUSES, RolloutCallback, apply_rollout_callback
from app.deps import get_control_plane_store
from app.routers.provisioning import _require_callback_auth

router = APIRouter(prefix="/api/rollouts", tags=["rollouts"])
_log = logging.getLogger("onebrain.rollouts")


class RolloutCallbackIn(BaseModel):
    status: str = Field(max_length=40)
    dry_run: bool = False
    external_run_id: str = Field(default="", max_length=200)
    external_run_url: str = Field(default="", max_length=500)
    migration_revision: str = Field(default="", max_length=120)
    smoke_status: str = Field(default="", max_length=80)
    failure_reason: str = Field(default="", max_length=1000)
    result_payload: dict = Field(default_factory=dict)


@router.post("/{rollout_id}/callback")
def rollout_callback(
    rollout_id: str,
    body: RolloutCallbackIn,
    authorization: str = Header(default=""),
    x_onebrain_callback_key_id: str = Header(default=""),
):
    _require_callback_auth(authorization, x_onebrain_callback_key_id)
    try:
        rollout = apply_rollout_callback(
            get_control_plane_store(), rollout_id, RolloutCallback(**body.model_dump())
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Rollout not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    reconcile_rollout_promotion(get_control_plane_store(), rollout)

    # If this child rollout just reached a terminal state and belongs to a fleet
    # rollout, advance the parent (pause / open next ring / complete). Never let an
    # advance error fail the child's callback ack — the child already recorded its
    # result and the operator can reconcile via resume.
    if rollout.exec_status in TERMINAL_EXEC_STATUSES and rollout.fleet_rollout_id:
        from app.routers.operator import fleet_dispatch_child

        try:
            advance_fleet_on_child(
                get_control_plane_store(), get_control_plane_store(), rollout,
                dispatch_child=fleet_dispatch_child)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("Fleet rollout advance failed for %s: %s", rollout.fleet_rollout_id, exc)

    return {
        "rollout_id": rollout.id,
        "exec_status": rollout.exec_status,
        "status": rollout.status,
    }

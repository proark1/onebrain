"""Session endpoints — expose roles/locations and the current principal."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.principal import Principal, resolve_principal
from app.auth.roles import LOCATIONS, ROLES
from app.schemas import RoleInfo, SessionInfo

router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("/roles", response_model=list[RoleInfo])
def list_roles():
    return [
        RoleInfo(id=r.id, label=r.label, clearance=r.clearance.name.lower(), scope=r.scope)
        for r in ROLES.values()
    ]


@router.get("/locations", response_model=list[str])
def list_locations():
    return LOCATIONS


@router.get("/me", response_model=SessionInfo)
def me(principal: Principal = Depends(resolve_principal)):
    return SessionInfo(
        role_id=principal.role_id,
        role_label=principal.role_label,
        clearance=principal.clearance.name.lower(),
        location_label=principal.location_label,
        display_name=principal.display_name,
        email=principal.email,
    )

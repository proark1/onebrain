"""The authenticated caller.

`resolve_principal` is the ONLY place identity enters the app. It authenticates
from a signed session cookie (set at login), loads the user, and builds the
Principal from the USER ACCOUNT — role/tenant/location are never caller-supplied.
Unauthenticated requests fail closed with 401. Everything downstream depends on
`Principal`, not on how it was obtained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, HTTPException

from app.auth.roles import ROLES
from app.auth.tokens import read_token
from app.security.policy import AccessFilter, Classification

HUMAN_TENANT = "nft_gym"  # default tenant for accounts that don't specify one
SESSION_COOKIE = "ob_session"


# The human/header path is PINNED to this tenant. A second business (Company B)
# is never reachable via headers — it is served only through scoped service keys
# (see the omnichannel plan). This stays hard-coded until OIDC binds tenant to a
# signed, server-side-allow-listed claim.
HUMAN_TENANT = "nft_gym"


@dataclass(frozen=True)
class Principal:
    user_id: str
    role_id: str
    role_label: str
    clearance: Classification
    locations: Optional[frozenset]   # None = all locations
    categories: Optional[frozenset]  # None = all categories
    location_label: str
    tenant_id: str = HUMAN_TENANT
    principal_type: str = "human"    # "human" | "service" (service keys land in Phase 1)
    display_name: str = ""
    email: str = ""

    @property
    def is_employee(self) -> bool:
        return self.role_id != "public"

    def access_filter(self) -> AccessFilter:
        return AccessFilter(self.tenant_id, int(self.clearance), self.locations, self.categories)


def principal_from_user(user) -> Principal:
    """Build a Principal from a user account. Role/tenant/location come from the
    account, never from the request — so nothing here is caller-controlled."""
    role = ROLES.get(user.role_id) or ROLES["public"]
    location = (user.location or "").strip().lower()

    if role.scope == "chain":
        locations: Optional[frozenset] = None
        location_label = "all locations"
    elif role.scope == "location":
        locations = frozenset({location})
        location_label = location
    else:
        locations = frozenset()
        location_label = "—"

    return Principal(
        user_id=user.id,
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=locations,
        categories=role.categories,
        location_label=location_label,
        tenant_id=user.tenant_id or HUMAN_TENANT,
        display_name=user.display_name,
        email=user.email,
    )


def resolve_principal(ob_session: str = Cookie(default="")) -> Principal:
    from app.config import get_settings
    from app.deps import get_user_store

    user_id = read_token(ob_session, get_settings().auth_secret) if ob_session else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = get_user_store().get(user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=401, detail="Not authenticated")

    return principal_from_user(user)

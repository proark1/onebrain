"""The authenticated caller.

`resolve_principal` is the ONLY place identity enters the app. For the prototype
it reads a role + location from request headers (driven by the UI's role
switcher). Swapping in real OIDC/JWT later means changing only this function —
everything downstream depends on `Principal`, not on how it was obtained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Header

from app.auth.roles import DEFAULT_ROLE, ROLES
from app.security.policy import AccessFilter, Classification


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

    @property
    def is_employee(self) -> bool:
        return self.role_id != "public"

    def access_filter(self) -> AccessFilter:
        return AccessFilter(self.tenant_id, int(self.clearance), self.locations, self.categories)


def resolve_principal(
    x_onebrain_role: str = Header(default=DEFAULT_ROLE),
    x_onebrain_location: str = Header(default="munich"),
) -> Principal:
    role = ROLES.get(x_onebrain_role) or ROLES[DEFAULT_ROLE]
    location = (x_onebrain_location or "munich").strip().lower()

    if role.scope == "chain":
        locations: Optional[frozenset] = None
        location_label = "all locations"
    elif role.scope == "location":
        locations = frozenset({location})
        location_label = location
    else:  # public
        locations = frozenset()
        location_label = "—"

    return Principal(
        user_id=f"{role.id}@{location}",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=locations,
        categories=role.categories,
        location_label=location_label,
    )

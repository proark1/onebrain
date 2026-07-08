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

from fastapi import Cookie, Header, HTTPException, Request

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
    principal_type: str = "human"    # "human" | "service"
    display_name: str = ""
    email: str = ""
    scopes: frozenset = frozenset()  # service-key scopes; humans hold none
    account_id: str = ""
    space_ids: Optional[frozenset] = None
    app_id: str = ""
    purposes: Optional[frozenset] = None

    @property
    def is_employee(self) -> bool:
        # Only a human account is ever an "employee". A service key is never one,
        # even though it has a non-public role_id — human endpoints must not treat
        # it as staff.
        return self.principal_type == "human" and self.role_id != "public"

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def access_filter(self) -> AccessFilter:
        return AccessFilter(
            self.tenant_id, int(self.clearance), self.locations, self.categories,
            account_id=self.account_id, space_ids=self.space_ids,
        )


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


# The service surface. A service key never conveys a role or a clearance above
# PUBLIC, and its tenant comes from the key record — NEVER from the caller. This
# is the hard ceiling that lets an untrusted comms adapter / partner service talk
# to the brain without ever being able to read internal data.
SERVICE_USAGE_ENDPOINTS = {
    "capabilities": "service.capabilities",
    "intake": "service.intake",
    "capture": "service.capture",
    "service_brand_theme": "service.brand_theme",
    "update_service_brand_theme": "service.brand_theme.update",
    "service_ask": "service.ask",
    "create_assistant_record": "service.assistant.records.create",
    "list_assistant_records": "service.assistant.records.list",
    "get_assistant_record": "service.assistant.records.read",
    "record_assistant_audit_event": "service.assistant.audit",
    "get_job": "jobs.read",
}


def _usage_endpoint_from_request(request: Request | None) -> str:
    if request is None:
        return "service.auth"
    endpoint = request.scope.get("endpoint")
    name = getattr(endpoint, "__name__", "")
    return SERVICE_USAGE_ENDPOINTS.get(name, "service.unknown")


def service_principal_from_authorization(authorization: str, usage_endpoint: str = "service.auth") -> Principal:
    from app.deps import get_service_key_store
    from app.servicekeys.base import parse_key, verify_secret

    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    parsed = parse_key(token) if token else None
    if not parsed:
        raise HTTPException(status_code=401, detail="Missing or malformed service key")

    key_id, secret = parsed
    key = get_service_key_store().get(key_id)
    # Fail closed: unknown key, revoked key, or a secret mismatch all deny.
    if not key or key.status != "active" or not verify_secret(secret, key.key_hash):
        raise HTTPException(status_code=401, detail="Invalid service key")
    key = get_service_key_store().record_usage(key.id, usage_endpoint)

    return Principal(
        user_id=f"svc:{key.id}",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,     # hard PUBLIC ceiling, never elevatable
        locations=frozenset(),               # global-only
        categories=frozenset({"general"}),   # general compartment only — no captured_input
        location_label="—",
        tenant_id=key.tenant_id,             # pinned by the key, not the caller
        principal_type="service",
        scopes=frozenset(key.scopes),
        display_name=key.label,
        account_id=key.account_id,
        space_ids=frozenset(key.space_ids) if key.space_ids else None,
        app_id=key.app_id,
        purposes=frozenset(key.purposes) if key.purposes else None,
    )


def resolve_service_principal(
    authorization: str = Header(default=""),
    request: Request = None,
) -> Principal:
    return service_principal_from_authorization(authorization, _usage_endpoint_from_request(request))

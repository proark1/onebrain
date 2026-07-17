"""Tenant-local bootstrap for customer-shaped OneBrain deployments.

Mission Control records the provisioning intent, but an isolated customer box
owns a different database.  The small, non-secret descriptor defined here lets
that box recreate only its local platform topology.  Raw integration keys arrive
through separate secret environment values and are stored hash-only.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, replace
from typing import Mapping

from app.platform.base import (
    ACCOUNT_KINDS,
    Account,
    AppInstallation,
    AuditEvent,
    Space,
    default_brand_theme,
)
from app.provisioning.bundles import get_bundle
from app.provisioning.service import PURPOSE_SCOPES
from app.servicekeys.base import ServiceKey, hash_secret, parse_key


BOOTSTRAP_SCHEMA_VERSION = 1
MAX_BOOTSTRAP_ENCODED_BYTES = 4096
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_FIELDS = frozenset({"schema_version", "account_id", "account_kind", "customer_name", "bundle_id"})
_LOCAL_INTEGRATION_APPS = ("assistant", "communication")


@dataclass(frozen=True)
class CustomerBootstrapDescriptor:
    account_id: str
    account_kind: str
    customer_name: str
    bundle_id: str
    schema_version: int = BOOTSTRAP_SCHEMA_VERSION


@dataclass(frozen=True)
class CustomerBootstrapResult:
    account_id: str
    spaces: int
    apps: int
    integration_keys: int
    administrator_rebound: bool = False


def _validated_descriptor(descriptor: CustomerBootstrapDescriptor) -> CustomerBootstrapDescriptor:
    account_id = (descriptor.account_id or "").strip()
    account_kind = (descriptor.account_kind or "").strip()
    customer_name = (descriptor.customer_name or "").strip()
    bundle_id = (descriptor.bundle_id or "").strip()
    if descriptor.schema_version != BOOTSTRAP_SCHEMA_VERSION:
        raise ValueError(f"Unsupported customer bootstrap schema version: {descriptor.schema_version}")
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("Customer bootstrap account id is invalid.")
    if account_kind not in ACCOUNT_KINDS:
        raise ValueError(f"Customer bootstrap account kind is invalid: {account_kind}")
    if not customer_name or len(customer_name) > 200 or any(ord(ch) < 32 for ch in customer_name):
        raise ValueError("Customer bootstrap customer name is invalid.")
    get_bundle(bundle_id)
    return CustomerBootstrapDescriptor(
        account_id=account_id,
        account_kind=account_kind,
        customer_name=customer_name,
        bundle_id=bundle_id,
    )


def encode_customer_bootstrap(descriptor: CustomerBootstrapDescriptor) -> str:
    descriptor = _validated_descriptor(descriptor)
    body = json.dumps(
        {
            "account_id": descriptor.account_id,
            "account_kind": descriptor.account_kind,
            "bundle_id": descriptor.bundle_id,
            "customer_name": descriptor.customer_name,
            "schema_version": descriptor.schema_version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    if len(encoded) > MAX_BOOTSTRAP_ENCODED_BYTES:
        raise ValueError("Customer bootstrap descriptor is too large.")
    return encoded


def decode_customer_bootstrap(encoded: str) -> CustomerBootstrapDescriptor | None:
    value = (encoded or "").strip()
    if not value:
        return None
    if len(value) > MAX_BOOTSTRAP_ENCODED_BYTES:
        raise ValueError("Customer bootstrap descriptor is too large.")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Customer bootstrap descriptor is not valid base64.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Customer bootstrap descriptor is not valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("Customer bootstrap descriptor fields are invalid.")
    try:
        descriptor = CustomerBootstrapDescriptor(
            schema_version=int(payload["schema_version"]),
            account_id=str(payload["account_id"]),
            account_kind=str(payload["account_kind"]),
            customer_name=str(payload["customer_name"]),
            bundle_id=str(payload["bundle_id"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Customer bootstrap descriptor fields are invalid.") from exc
    return _validated_descriptor(descriptor)


def _scopes_for(purposes: tuple[str, ...]) -> tuple[str, ...]:
    scopes: list[str] = []
    for purpose in purposes:
        scopes.extend(PURPOSE_SCOPES.get(purpose, ()))
    return tuple(dict.fromkeys(scopes))


def _ensure_integration_key(store, key: ServiceKey) -> ServiceKey:
    existing = store.get(key.id)
    if existing:
        if existing.key_hash != key.key_hash:
            raise ValueError(f"Customer bootstrap service key id collision: {key.id}")
        if (
            existing.tenant_id != key.tenant_id
            or existing.account_id != key.account_id
            or existing.app_id != key.app_id
            or tuple(existing.space_ids) != tuple(key.space_ids)
            or tuple(existing.purposes) != tuple(key.purposes)
            or tuple(existing.scopes) != tuple(key.scopes)
        ):
            raise ValueError(f"Customer bootstrap service key metadata mismatch: {key.id}")
        return existing
    try:
        return store.create(key)
    except ValueError:
        existing = store.get(key.id)
        if existing and existing.key_hash == key.key_hash:
            return existing
        raise


def _record_bootstrap_audit_once(platform_store, event: AuditEvent) -> None:
    if any(existing.id == event.id for existing in platform_store.list_audit(event.account_id)):
        return
    try:
        platform_store.record_audit(event)
    except ValueError:
        if not any(existing.id == event.id for existing in platform_store.list_audit(event.account_id)):
            raise


def reconcile_customer_bootstrap(
    descriptor: CustomerBootstrapDescriptor,
    *,
    platform_store,
    service_key_store,
    user_store,
    session_store,
    administrator_email: str,
    integration_keys: Mapping[str, str],
) -> CustomerBootstrapResult:
    """Converge a customer database to its explicit provisioning bundle."""
    descriptor = _validated_descriptor(descriptor)
    bundle = get_bundle(descriptor.bundle_id)
    expected_integrations = [app_id for app_id in _LOCAL_INTEGRATION_APPS if any(
        app.app_id == app_id for app in bundle.apps
    )]
    raw_keys = {app_id: (integration_keys.get(app_id) or "").strip() for app_id in expected_integrations}
    missing = [app_id for app_id, raw in raw_keys.items() if not raw]
    if missing:
        raise ValueError(f"Customer bootstrap is missing integration keys: {','.join(missing)}")
    if len(set(raw_keys.values())) != len(raw_keys):
        raise ValueError("Customer bootstrap integration keys must be distinct.")

    admin_email = (administrator_email or "").strip().lower()
    administrator = user_store.get_by_email(admin_email) if admin_email else None
    administrator_rebound = False
    if administrator:
        if administrator.role_id != "admin":
            raise ValueError("Configured customer bootstrap administrator is not an admin.")
        if administrator.tenant_id == "nft_gym" and administrator.tenant_id != descriptor.account_id:
            administrator = user_store.update_scope(
                administrator.id,
                tenant_id=descriptor.account_id,
                role_id=administrator.role_id,
                location=administrator.location,
            )
            session_store.revoke_all_for_user(administrator.id)
            administrator_rebound = True
        elif administrator.tenant_id != descriptor.account_id:
            raise ValueError("Configured customer bootstrap administrator belongs to another account.")

    owner_user_id = administrator.id if administrator else admin_email
    platform_store.upsert_bootstrap_account(Account(
        id=descriptor.account_id,
        kind=descriptor.account_kind,
        name=descriptor.customer_name,
        owner_user_id=owner_user_id,
    ))

    spaces_by_key: dict[str, Space] = {}
    for template in bundle.spaces:
        space = platform_store.upsert_bootstrap_space(Space(
            id=f"sp_{descriptor.account_id}_{template.key}",
            account_id=descriptor.account_id,
            kind=template.kind,
            name=template.name,
        ))
        spaces_by_key[template.key] = space

    app_templates = {template.app_id: template for template in bundle.apps}
    for template in bundle.apps:
        platform_store.upsert_bootstrap_installation(AppInstallation(
            id=f"appi_{descriptor.account_id}_{template.app_id}",
            account_id=descriptor.account_id,
            app_id=template.app_id,
            enabled_space_ids=tuple(spaces_by_key[key].id for key in template.space_keys),
            allowed_purposes=template.purposes,
            display_name=template.display_name,
        ))

    platform_store.upsert_brand_theme(replace(
        default_brand_theme(descriptor.account_id),
        name=f"{descriptor.customer_name} brand",
        source="bootstrap",
    ))

    installed_key_count = 0
    for app_id, raw in raw_keys.items():
        parsed = parse_key(raw)
        if not parsed:
            raise ValueError(f"Customer bootstrap {app_id} integration key is malformed.")
        key_id, secret = parsed
        template = app_templates[app_id]
        _ensure_integration_key(service_key_store, ServiceKey(
            id=key_id,
            key_hash=hash_secret(secret),
            tenant_id=descriptor.account_id,
            scopes=_scopes_for(template.purposes),
            label=f"{template.display_name} integration",
            account_id=descriptor.account_id,
            app_id=app_id,
            space_ids=tuple(spaces_by_key[key].id for key in template.space_keys),
            purposes=template.purposes,
        ))
        installed_key_count += 1

    _record_bootstrap_audit_once(platform_store, AuditEvent(
        id=f"aud_bootstrap_{descriptor.account_id}",
        account_id=descriptor.account_id,
        actor_id=owner_user_id or "customer-bootstrap",
        actor_type="system",
        action="customer.bootstrap_reconciled",
        target_type="account",
        target_id=descriptor.account_id,
        decision="allowed",
        meta={"bundle_id": descriptor.bundle_id, "schema_version": descriptor.schema_version},
    ))

    return CustomerBootstrapResult(
        account_id=descriptor.account_id,
        spaces=len(bundle.spaces),
        apps=len(bundle.apps),
        integration_keys=installed_key_count,
        administrator_rebound=administrator_rebound,
    )

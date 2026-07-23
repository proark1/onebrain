"""Tenant-local bootstrap for customer-shaped OneBrain deployments.

Mission Control records the provisioning intent, but an isolated customer box
owns a different database. The small, non-secret descriptor defined here lets
that box recreate only its local platform topology. Raw integration keys arrive
through separate secret environment values and are stored hash-only.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, replace
from typing import Mapping

from app.accounting.base import ACCOUNTING_APP_ID, accounting_category_id
from app.platform.base import (
    ACCOUNT_KINDS,
    AccessGroup,
    AccessGroupMembership,
    Account,
    AppInstallation,
    AuditEvent,
    Space,
    default_brand_theme,
)
from app.provisioning.bundles import resolve_module_composition
from app.provisioning.service import PURPOSE_SCOPES
from app.servicekeys.base import ServiceKey, hash_secret, parse_key


BOOTSTRAP_SCHEMA_VERSION = 2
MAX_BOOTSTRAP_ENCODED_BYTES = 4096
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_FIELDS = frozenset({"schema_version", "account_id", "account_kind", "customer_name", "module_ids"})
_LOCAL_INTEGRATION_APPS = ("assistant", "communication")


@dataclass(frozen=True)
class CustomerBootstrapDescriptor:
    account_id: str
    account_kind: str
    customer_name: str
    module_ids: tuple[str, ...] = ()
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
    if descriptor.schema_version != BOOTSTRAP_SCHEMA_VERSION:
        raise ValueError(f"Unsupported customer bootstrap schema version: {descriptor.schema_version}")
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("Customer bootstrap account id is invalid.")
    if account_kind not in ACCOUNT_KINDS:
        raise ValueError(f"Customer bootstrap account kind is invalid: {account_kind}")
    if not customer_name or len(customer_name) > 200 or any(ord(ch) < 32 for ch in customer_name):
        raise ValueError("Customer bootstrap customer name is invalid.")
    module_ids = descriptor.module_ids
    if isinstance(module_ids, str) or not isinstance(module_ids, (list, tuple)):
        raise ValueError("Customer bootstrap module ids must be a list or tuple.")
    try:
        composition = resolve_module_composition(module_ids)
    except ValueError as exc:
        raise ValueError(f"Customer bootstrap module ids are invalid: {exc}") from exc
    return CustomerBootstrapDescriptor(
        account_id=account_id,
        account_kind=account_kind,
        customer_name=customer_name,
        module_ids=composition.selected_module_ids,
    )


def encode_customer_bootstrap(descriptor: CustomerBootstrapDescriptor) -> str:
    descriptor = _validated_descriptor(descriptor)
    body = json.dumps(
        {
            "account_id": descriptor.account_id,
            "account_kind": descriptor.account_kind,
            "customer_name": descriptor.customer_name,
            "module_ids": list(descriptor.module_ids),
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
    module_ids = payload.get("module_ids")
    if not isinstance(module_ids, list) or any(not isinstance(module_id, str) for module_id in module_ids):
        raise ValueError("Customer bootstrap descriptor module ids are invalid.")
    try:
        descriptor = CustomerBootstrapDescriptor(
            schema_version=int(payload["schema_version"]),
            account_id=str(payload["account_id"]),
            account_kind=str(payload["account_kind"]),
            customer_name=str(payload["customer_name"]),
            module_ids=tuple(module_ids),
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


def _seed_accounting_category(
    platform_store,
    *,
    account_id: str,
    template,
    spaces_by_key: Mapping[str, Space],
    owner_user_id: str,
    seed_membership: bool,
) -> None:
    """Create the ``buchhaltung`` Drive AccessGroup (+ owner) where installed.

    The accounting file trigger recognises invoices by this deterministic
    per-space AccessGroup id, so it must exist from install (plan §3/§4) — a bare
    AppInstallation is not enough. Idempotent: re-reconcile upserts by the same id.
    Admins already see the category regardless, so a membership is only seeded when
    a real owner user exists (a non-admin finance user still needs it).
    """
    if template is None:
        return
    for key in template.space_keys:
        space = spaces_by_key.get(key)
        if not space:
            continue
        group_id = accounting_category_id(space.id)
        platform_store.upsert_access_group(AccessGroup(
            id=group_id,
            account_id=account_id,
            name="Buchhaltung",
            kind="department",
            space_id=space.id,
        ))
        if seed_membership and owner_user_id:
            platform_store.upsert_access_group_membership(AccessGroupMembership(
                id=f"agm_{space.id}_buchhaltung_owner",
                account_id=account_id,
                group_id=group_id,
                user_id=owner_user_id,
                space_id=space.id,
            ))


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
    """Converge a customer database to its explicit product-module selection."""
    descriptor = _validated_descriptor(descriptor)
    composition = resolve_module_composition(descriptor.module_ids)
    expected_integrations = [
        app_id
        for app_id in _LOCAL_INTEGRATION_APPS
        if any(app.app_id == app_id for app in composition.apps)
    ]
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
    for template in composition.spaces:
        space = platform_store.upsert_bootstrap_space(Space(
            id=f"sp_{descriptor.account_id}_{template.key}",
            account_id=descriptor.account_id,
            kind=template.kind,
            name=template.name,
        ))
        spaces_by_key[template.key] = space

    app_templates = {template.app_id: template for template in composition.apps}
    for template in composition.apps:
        platform_store.upsert_bootstrap_installation(AppInstallation(
            id=f"appi_{descriptor.account_id}_{template.app_id}",
            account_id=descriptor.account_id,
            app_id=template.app_id,
            enabled_space_ids=tuple(spaces_by_key[key].id for key in template.space_keys),
            allowed_purposes=template.purposes,
            display_name=template.display_name,
        ))

    _seed_accounting_category(
        platform_store,
        account_id=descriptor.account_id,
        template=app_templates.get(ACCOUNTING_APP_ID),
        spaces_by_key=spaces_by_key,
        owner_user_id=owner_user_id,
        seed_membership=administrator is not None,
    )

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
        meta={"module_ids": list(descriptor.module_ids), "schema_version": descriptor.schema_version},
    ))

    return CustomerBootstrapResult(
        account_id=descriptor.account_id,
        spaces=len(composition.spaces),
        apps=len(composition.apps),
        integration_keys=installed_key_count,
        administrator_rebound=administrator_rebound,
    )

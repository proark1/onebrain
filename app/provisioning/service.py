"""Provision customer accounts across platform and operator stores."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.auth.passwords import hash_password
from app.controlplane.base import CustomerDeployment, DeploymentModule, validate_deployment, validate_module
from app.platform.base import Account, AppInstallation, AuditEvent, BrandTheme, BRAND_COLOR_FIELDS, Space, default_brand_theme
from app.provisioning.bundles import ProvisioningBundle, get_bundle
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE, ServiceKey, generate_key, hash_secret
from app.users.base import User


_ID_RE = re.compile(r"[^a-z0-9_]+")


def normalize_id(value: str) -> str:
    normalized = _ID_RE.sub("_", (value or "").strip().lower()).strip("_")
    if not normalized:
        raise ValueError("A non-empty id is required.")
    return normalized


@dataclass(frozen=True)
class ProvisionedCredential:
    id: str
    key: str
    tenant_id: str
    account_id: str
    app_id: str
    label: str
    scopes: List[str]
    space_ids: List[str]
    purposes: List[str]


@dataclass(frozen=True)
class ProvisioningResult:
    bundle: ProvisioningBundle
    account: Account
    spaces: List[Space]
    installations: List[AppInstallation]
    deployment: CustomerDeployment
    modules: List[DeploymentModule]
    credentials: List[ProvisionedCredential]
    brand_theme: BrandTheme
    app_brand_themes: List[BrandTheme]
    # H-10: the owner's one-time password, returned ONCE when an owner is minted
    # (owner_email set). Stored hash-only on the User; never persisted in plaintext.
    owner_one_time_password: str = ""


PURPOSE_SCOPES = {
    "assistant_context": (SCOPE_READ,),
    "assistant_action": (SCOPE_WRITE,),
    "customer_service_answer": (SCOPE_READ,),
    "customer_service_inbox": (SCOPE_WRITE,),
    "kpi_read": (SCOPE_READ,),
    "kpi_configure": (SCOPE_WRITE,),
    "kpi_snapshot_write": (SCOPE_WRITE,),
    "ai_employee_read": (SCOPE_READ,),
    "ai_employee_configure": (SCOPE_WRITE,),
    "ai_employee_action_propose": (SCOPE_WRITE,),
    "ai_employee_action_approve": (SCOPE_WRITE,),
}

EXTERNAL_CREDENTIAL_APPS = frozenset({"assistant", "communication", "kpi_dashboard"})


class CustomerProvisioner:
    def __init__(self, platform_store, control_plane_store, service_key_store=None, user_store=None):
        self.platform_store = platform_store
        self.control_plane_store = control_plane_store
        self.service_key_store = service_key_store
        self.user_store = user_store

    def _scopes_for(self, purposes: tuple[str, ...]) -> tuple[str, ...]:
        scopes: list[str] = []
        for purpose in purposes:
            scopes.extend(PURPOSE_SCOPES.get(purpose, ()))
        return tuple(dict.fromkeys(scopes))

    def _mint_credentials(self, account_id: str, installations: list[AppInstallation]) -> list[ProvisionedCredential]:
        if not self.service_key_store:
            return []

        credentials: list[ProvisionedCredential] = []
        for installation in installations:
            if installation.app_id not in EXTERNAL_CREDENTIAL_APPS:
                continue
            credential_purposes = (
                ("kpi_snapshot_write",)
                if installation.app_id == "kpi_dashboard"
                else installation.allowed_purposes
            )
            scopes = self._scopes_for(credential_purposes)
            if not scopes:
                continue

            label = f"{installation.display_name or installation.app_id} integration"
            key_id, secret, plaintext = generate_key()
            self.service_key_store.create(ServiceKey(
                id=key_id,
                key_hash=hash_secret(secret),
                tenant_id=account_id,
                scopes=scopes,
                label=label,
                account_id=account_id,
                app_id=installation.app_id,
                space_ids=installation.enabled_space_ids,
                purposes=credential_purposes,
            ))
            credentials.append(ProvisionedCredential(
                id=key_id,
                key=plaintext,
                tenant_id=account_id,
                account_id=account_id,
                app_id=installation.app_id,
                label=label,
                scopes=list(scopes),
                space_ids=list(installation.enabled_space_ids),
                purposes=list(credential_purposes),
            ))
        return credentials

    def _scoped_theme(
        self,
        *,
        account_id: str,
        app_id: str = "",
        customer_name: str,
        theme: BrandTheme | None = None,
        source: str,
    ) -> BrandTheme:
        base = default_brand_theme(account_id, app_id)
        colors = {field: getattr(theme, field) if theme else getattr(base, field) for field in BRAND_COLOR_FIELDS}
        return BrandTheme(
            id=f"brand_{account_id}_{app_id or 'account'}",
            account_id=account_id,
            app_id=app_id,
            name=(theme.name if theme and theme.name else f"{customer_name} brand").strip(),
            logo_url=(theme.logo_url if theme else base.logo_url).strip(),
            source=source,
            status=theme.status if theme and theme.status else "active",
            **colors,
        )

    def provision(
        self,
        *,
        account_id: str,
        account_kind: str,
        customer_name: str,
        owner_user_id: str,
        bundle_id: str,
        deployment_id: str,
        deployment_type: str,
        region: str,
        release_ring: str,
        initial_version: str,
        environment: str = "production",
        current_migration: str = "",
        module_versions: Optional[Dict[str, str]] = None,
        mint_integration_keys: bool = False,
        brand_theme: BrandTheme | None = None,
        app_brand_themes: Optional[Dict[str, BrandTheme]] = None,
        owner_email: str = "",
    ) -> ProvisioningResult:
        bundle = get_bundle(bundle_id)
        account_id = normalize_id(account_id)
        deployment_id = normalize_id(deployment_id)
        customer_name = customer_name.strip()
        initial_version = initial_version.strip()
        module_versions = {
            normalize_id(module_id).replace("_", "-"): version.strip()
            for module_id, version in (module_versions or {}).items()
        }
        unknown_module_versions = sorted(set(module_versions) - set(bundle.modules))
        if unknown_module_versions:
            raise ValueError(f"Unknown module versions for this bundle: {unknown_module_versions}")
        app_brand_themes = app_brand_themes or {}
        bundle_app_ids = {app.app_id for app in bundle.apps}
        unknown_theme_apps = sorted(set(app_brand_themes) - bundle_app_ids)
        if unknown_theme_apps:
            raise ValueError(f"Unknown app theme overrides for this bundle: {unknown_theme_apps}")

        deployment = CustomerDeployment(
            id=deployment_id,
            customer_name=customer_name,
            account_id=account_id,
            environment=environment.strip(),
            deployment_type=deployment_type.strip(),
            region=region.strip(),
            release_ring=release_ring.strip(),
            current_version=initial_version,
            current_migration=current_migration.strip(),
        )
        modules = [
            DeploymentModule(
                deployment_id=deployment_id,
                module_id=module_id,
                version=module_versions.get(module_id, initial_version),
            )
            for module_id in bundle.modules
        ]

        validate_deployment(deployment)
        for module in modules:
            validate_module(module)
            if not module.version.strip():
                raise ValueError("Module versions must be non-empty.")
        if self.platform_store.get_account(account_id):
            raise ValueError(f"account already exists: {account_id}")
        if self.control_plane_store.get_deployment(deployment_id):
            raise ValueError(f"deployment already exists: {deployment_id}")

        account = self.platform_store.create_account(Account(
            id=account_id,
            kind=account_kind.strip(),
            name=customer_name,
            owner_user_id=owner_user_id,
        ))

        # H-10: when an owner email is supplied (and a user store is wired), mint
        # the owner admin with a random one-time password stored HASH-ONLY +
        # must_change_password=True. The plaintext OTP is returned ONCE on the
        # result (the caller wraps it in a short-TTL OneTimeSecretEnvelope, A8);
        # it is never persisted in plaintext anywhere.
        owner_one_time_password = ""
        normalized_owner_email = (owner_email or "").strip().lower()
        if normalized_owner_email and self.user_store is not None:
            owner_one_time_password = secrets.token_urlsafe(18)
            self.user_store.create(User(
                id=f"usr_owner_{account_id}",
                email=normalized_owner_email,
                display_name=f"{customer_name} owner",
                password_hash=hash_password(owner_one_time_password),
                tenant_id=account_id,
                role_id="admin",
                location="",
                must_change_password=True,
            ))

        spaces_by_key: dict[str, Space] = {}
        for template in bundle.spaces:
            space = self.platform_store.create_space(Space(
                id=f"sp_{account_id}_{template.key}",
                account_id=account_id,
                kind=template.kind,
                name=template.name,
            ))
            spaces_by_key[template.key] = space

        installations: list[AppInstallation] = []
        for template in bundle.apps:
            installation = self.platform_store.install_app(AppInstallation(
                id=f"appi_{account_id}_{template.app_id}",
                account_id=account_id,
                app_id=template.app_id,
                enabled_space_ids=tuple(spaces_by_key[key].id for key in template.space_keys),
                allowed_purposes=template.purposes,
                display_name=template.display_name,
            ))
            installations.append(installation)

        created_brand_theme = self.platform_store.upsert_brand_theme(self._scoped_theme(
            account_id=account_id,
            customer_name=customer_name,
            theme=brand_theme,
            source="provisioning",
        ))
        created_app_brand_themes: list[BrandTheme] = []
        for installation in installations:
            override = app_brand_themes.get(installation.app_id)
            if override:
                created_app_brand_themes.append(self.platform_store.upsert_brand_theme(self._scoped_theme(
                    account_id=account_id,
                    app_id=installation.app_id,
                    customer_name=customer_name,
                    theme=override,
                    source="app_override",
                )))

        created_deployment = self.control_plane_store.create_deployment(deployment)
        created_modules = [self.control_plane_store.upsert_module(module) for module in modules]
        credentials = self._mint_credentials(account_id, installations) if mint_integration_keys else []
        resolved_app_brand_themes = [
            self.platform_store.resolve_brand_theme(account_id, installation.app_id)
            for installation in installations
        ]

        self.platform_store.record_audit(AuditEvent(
            id=f"aud_provision_{account_id}",
            account_id=account_id,
            actor_id=owner_user_id,
            actor_type="human",
            action="customer.provisioned",
            target_type="account",
            target_id=account_id,
            meta={
                "bundle_id": bundle.id,
                "deployment_id": created_deployment.id,
                "modules": {m.module_id: m.version for m in created_modules},
                "service_key_ids": [credential.id for credential in credentials],
                "brand_theme_id": created_brand_theme.id,
                "app_brand_theme_ids": [theme.id for theme in created_app_brand_themes],
            },
        ))

        return ProvisioningResult(
            bundle=bundle,
            account=account,
            spaces=list(spaces_by_key.values()),
            installations=installations,
            deployment=created_deployment,
            modules=created_modules,
            credentials=credentials,
            brand_theme=created_brand_theme,
            app_brand_themes=resolved_app_brand_themes,
            owner_one_time_password=owner_one_time_password,
        )

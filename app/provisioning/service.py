"""Provision customer accounts across platform and operator stores."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.controlplane.base import CustomerDeployment, DeploymentModule, validate_deployment, validate_module
from app.platform.base import Account, AppInstallation, AuditEvent, Space
from app.provisioning.bundles import ProvisioningBundle, get_bundle


_ID_RE = re.compile(r"[^a-z0-9_]+")


def normalize_id(value: str) -> str:
    normalized = _ID_RE.sub("_", (value or "").strip().lower()).strip("_")
    if not normalized:
        raise ValueError("A non-empty id is required.")
    return normalized


@dataclass(frozen=True)
class ProvisioningResult:
    bundle: ProvisioningBundle
    account: Account
    spaces: List[Space]
    installations: List[AppInstallation]
    deployment: CustomerDeployment
    modules: List[DeploymentModule]


class CustomerProvisioner:
    def __init__(self, platform_store, control_plane_store):
        self.platform_store = platform_store
        self.control_plane_store = control_plane_store

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
        current_migration: str = "",
        module_versions: Optional[Dict[str, str]] = None,
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

        deployment = CustomerDeployment(
            id=deployment_id,
            customer_name=customer_name,
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

        created_deployment = self.control_plane_store.create_deployment(deployment)
        created_modules = [self.control_plane_store.upsert_module(module) for module in modules]

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
            },
        ))

        return ProvisioningResult(
            bundle=bundle,
            account=account,
            spaces=list(spaces_by_key.values()),
            installations=installations,
            deployment=created_deployment,
            modules=created_modules,
        )

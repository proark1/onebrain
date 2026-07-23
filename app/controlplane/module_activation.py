"""Change an already-provisioned customer's product-module set (Phase 1: DB-only, add-only).

Modules are installed on a box from its customer bootstrap descriptor, which the box
reconciles on every boot (``app.main`` -> ``reconcile_customer_bootstrap``). This module
re-mints that descriptor into the deployment's re-fetched secret bundle and bumps
``secrets_epoch`` so the box re-fetches, restarts ``onebrain-api``, and its boot reconcile
upserts the new app installations.

Phase 1 is deliberately narrow and safe:

* **DB-only** — only modules that run *inside* the Core containers
  (``ProvisioningModule.modules`` is empty: KPI Dashboard, AI Employees, Buchhaltung).
  Activating them needs no new container, no new integration secret, and no compose change,
  so nothing is ever delivered to the live box beyond the re-fetched bundle. Service-backed
  modules (Assistant, Communication) are rejected here until the Phase-2 full-skeleton work.
* **Add-only** — the reconcile is upsert-only; removing a module (data + GDPR teardown) is
  a separate Phase-3 path, so a request that would drop a current module is rejected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from app.fleet.bootstrap_bundle import validate_bundle
from app.provisioning.bundles import OPTIONAL_MODULES, resolve_module_composition
from app.provisioning.customer_bootstrap import (
    decode_customer_bootstrap,
    encode_customer_bootstrap,
)
from app.provisioning.runs import OneTimeSecretCipher

_BUNDLE_DESCRIPTOR_KEY = "ONEBRAIN_CUSTOMER_BOOTSTRAP"

# Optional modules whose ProvisioningModule.modules is empty run in the Core containers, so
# they add only platform-DB rows — no container, no secret, no compose change. Derived from
# the catalogue so a future DB-only module is covered automatically.
TIER1_DB_ONLY_MODULE_IDS = frozenset(module.id for module in OPTIONAL_MODULES if not module.modules)


@dataclass(frozen=True)
class ModuleActivationResult:
    deployment_id: str
    selected_module_ids: tuple[str, ...]
    added_module_ids: tuple[str, ...]
    secrets_epoch: int
    changed: bool


def set_deployment_modules(
    *,
    deployment,
    desired_module_ids,
    provision_store,
    control_store,
    settings,
) -> ModuleActivationResult:
    """Converge a live customer deployment onto ``desired_module_ids`` (full optional set).

    Phase 1 rules: add-only (the new set must be a superset of the current one) and DB-only
    (assistant/communication are rejected). Idempotent — a no-op change returns the current
    epoch without a re-mint. Raises ``ValueError`` on any violated guardrail.
    """
    if deployment is None:
        raise ValueError("unknown deployment")
    if deployment.removed_at:
        raise ValueError("deployment is decommissioned")
    if deployment.is_release_gate:
        raise ValueError(
            "a development gate's module set is fixed by its provisioner, not editable here"
        )

    # Validate + normalise the requested set (rejects unknown/duplicate ids).
    composition = resolve_module_composition(desired_module_ids)
    desired = frozenset(composition.selected_module_ids)
    current = frozenset(deployment.selected_module_ids)

    removed = current - desired
    if removed:
        raise ValueError(
            f"module removal is not supported yet (Phase 3): {sorted(removed)}"
        )
    added = desired - current
    service_backed = sorted(added - TIER1_DB_ONLY_MODULE_IDS)
    if service_backed:
        raise ValueError(
            "service-backed modules cannot be activated in place yet (Phase 2): "
            f"{service_backed}"
        )

    if not added:
        bundle_row = provision_store.get_secret_bundle(deployment.id)
        epoch = bundle_row.secrets_epoch if bundle_row else 0
        return ModuleActivationResult(
            deployment.id, tuple(composition.selected_module_ids), (), epoch, False
        )

    # Re-mint the descriptor into the sealed, re-fetched bundle. Mirrors the gate-adoption
    # re-seal + epoch-bump, including the rollback that restores the prior ciphertext if the
    # epoch bump fails (a served-but-un-bumped epoch would never trigger the box's re-fetch).
    bundle_row = provision_store.get_secret_bundle(deployment.id)
    if bundle_row is None:
        raise ValueError("deployment has no secret bundle")
    cipher = OneTimeSecretCipher(settings)
    try:
        bundle = json.loads(cipher.open_bundle(bundle_row.ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("deployment secret bundle could not be opened") from exc
    if not isinstance(bundle, dict):
        raise ValueError("deployment secret bundle could not be opened")

    descriptor = decode_customer_bootstrap(bundle.get(_BUNDLE_DESCRIPTOR_KEY, ""))
    if descriptor is None:
        raise ValueError(
            "deployment bundle carries no module descriptor; the box predates "
            "bundle-delivered modules and must be re-provisioned"
        )

    new_descriptor = replace(descriptor, module_ids=composition.selected_module_ids)
    bundle[_BUNDLE_DESCRIPTOR_KEY] = encode_customer_bootstrap(new_descriptor)

    errors = validate_bundle(bundle)
    if errors:
        raise ValueError(f"deployment secret bundle is invalid: {errors[0]}")

    sealed = cipher.seal_bundle(json.dumps(bundle, separators=(",", ":"), sort_keys=True))
    provision_store.upsert_secret_bundle(replace(bundle_row, ciphertext=sealed))
    try:
        epoch = provision_store.bump_secrets_epoch(deployment.id)
    except Exception:
        provision_store.upsert_secret_bundle(bundle_row)  # restore the prior ciphertext
        raise

    # The box's bundle descriptor is now the source of truth; reflect it in MC metadata so the
    # operator view and later reconciles agree with what the box will install.
    control_store.update_deployment_modules(
        deployment.id, selected_module_ids=composition.selected_module_ids
    )
    return ModuleActivationResult(
        deployment.id,
        tuple(composition.selected_module_ids),
        tuple(sorted(added)),
        epoch,
        True,
    )

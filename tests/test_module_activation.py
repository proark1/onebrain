from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.module_activation import (
    TIER1_DB_ONLY_MODULE_IDS,
    set_deployment_modules,
)
from app.platform.base import Account
from app.platform.memory import MemoryPlatformStore
from app.routers import operator as operator_router
from app.provisioning.customer_bootstrap import (
    CustomerBootstrapDescriptor,
    decode_customer_bootstrap,
    encode_customer_bootstrap,
)
from app.provisioning.runs import (
    BoxSecretBundle,
    MemoryProvisioningRunStore,
    OneTimeSecretCipher,
)


def _settings():
    return SimpleNamespace(
        secret_encryption_key="test-module-activation-key",
        secret_encryption_key_version="v1",
        bootstrap_secret_ttl_seconds=3600,
    )


def _bundle(descriptor_module_ids=()):
    return {
        "POSTGRES_PASSWORD": "p" * 32,
        "POSTGRES_APP_PASSWORD": "a" * 32,
        "POSTGRES_WORKER_PASSWORD": "w" * 32,
        "POSTGRES_ASSISTANT_PASSWORD": "s" * 32,
        "POSTGRES_COMMUNICATION_PASSWORD": "c" * 32,
        "REDIS_PASSWORD": "r" * 32,
        "ONEBRAIN_FLEET_KEY": "fleet-token",
        "ONEBRAIN_LLM_API_KEY": "",
        "ONEBRAIN_AUTH_SECRET": "h" * 32,
        "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET": "l" * 32,
        "ONEBRAIN_ADMIN_EMAIL": "owner@example.com",
        "ONEBRAIN_ADMIN_PASSWORD": "one-time-password",
        "ONEBRAIN_SERVICE_KEY": "",
        "ONEBRAIN_SPACE_ID": "",
        "ONEBRAIN_CUSTOMER_BOOTSTRAP": encode_customer_bootstrap(CustomerBootstrapDescriptor(
            account_id="acct_1",
            account_kind="organization",
            customer_name="Acme",
            module_ids=tuple(descriptor_module_ids),
        )),
        "UPDATE_BACKUP_KEY": "b" * 32,
        "UPDATE_DESIRED_STATE_PUBLIC_KEYS": "",
        "ONEBRAIN_DNS_TOKEN": "",
        "ONEBRAIN_BACKUP_S3_ACCESS_KEY": "",
        "ONEBRAIN_BACKUP_S3_SECRET_KEY": "",
    }


def _stores(*, selected=(), descriptor_module_ids=None, is_release_gate=False,
            removed_at="", seed_descriptor=True):
    settings = _settings()
    deployment = CustomerDeployment(
        id="dep_a",
        customer_name="Acme",
        account_id="acct_1",
        selected_module_ids=tuple(selected),
        is_release_gate=is_release_gate,
        removed_at=removed_at,
    )
    control = MemoryControlPlaneStore()
    control.create_deployment(deployment)
    prov = MemoryProvisioningRunStore()
    bundle = _bundle(selected if descriptor_module_ids is None else descriptor_module_ids)
    if not seed_descriptor:
        bundle.pop("ONEBRAIN_CUSTOMER_BOOTSTRAP", None)
    prov.upsert_secret_bundle(BoxSecretBundle(
        deployment_id=deployment.id,
        account_id=deployment.account_id,
        ciphertext=OneTimeSecretCipher(settings).seal_bundle(json.dumps(bundle)),
    ))
    return settings, deployment, prov, control


def _descriptor_in_bundle(prov, settings, deployment_id):
    row = prov.get_secret_bundle(deployment_id)
    bundle = json.loads(OneTimeSecretCipher(settings).open_bundle(row.ciphertext))
    return decode_customer_bootstrap(bundle["ONEBRAIN_CUSTOMER_BOOTSTRAP"])


def test_add_db_only_module_remints_descriptor_and_bumps_epoch():
    settings, deployment, prov, control = _stores(selected=())

    result = set_deployment_modules(
        deployment=deployment, desired_module_ids=["buchhaltung"],
        provision_store=prov, control_store=control, settings=settings,
    )

    assert result.changed is True
    assert result.added_module_ids == ("buchhaltung",)
    assert result.selected_module_ids == ("buchhaltung",)
    assert result.secrets_epoch == 1  # bump from 0 triggers the box re-fetch
    # The descriptor the box will reconcile from now carries the new module.
    assert _descriptor_in_bundle(prov, settings, "dep_a").module_ids == ("buchhaltung",)
    # MC metadata reflects the box's source of truth.
    assert control.get_deployment("dep_a").selected_module_ids == ("buchhaltung",)


def test_buchhaltung_kpi_ai_employees_are_tier1_db_only():
    assert TIER1_DB_ONLY_MODULE_IDS == frozenset({"kpi_dashboard", "ai_employees", "buchhaltung"})


def test_service_backed_module_is_rejected_until_phase2():
    settings, deployment, prov, control = _stores(selected=())
    with pytest.raises(ValueError, match="Phase 2"):
        set_deployment_modules(
            deployment=deployment, desired_module_ids=["communication"],
            provision_store=prov, control_store=control, settings=settings,
        )
    # Nothing was minted or recorded on a rejected request.
    assert prov.get_secret_bundle("dep_a").secrets_epoch == 0
    assert control.get_deployment("dep_a").selected_module_ids == ()


def test_unknown_module_is_rejected():
    settings, deployment, prov, control = _stores(selected=())
    with pytest.raises(ValueError, match="[Uu]nknown"):
        set_deployment_modules(
            deployment=deployment, desired_module_ids=["not_a_module"],
            provision_store=prov, control_store=control, settings=settings,
        )


def test_removal_is_rejected_until_phase3():
    settings, deployment, prov, control = _stores(selected=("buchhaltung",))
    with pytest.raises(ValueError, match="Phase 3"):
        set_deployment_modules(
            deployment=deployment, desired_module_ids=[],
            provision_store=prov, control_store=control, settings=settings,
        )


def test_noop_change_does_not_bump_epoch():
    settings, deployment, prov, control = _stores(selected=("buchhaltung",))
    result = set_deployment_modules(
        deployment=deployment, desired_module_ids=["buchhaltung"],
        provision_store=prov, control_store=control, settings=settings,
    )
    assert result.changed is False
    assert result.secrets_epoch == 0
    assert prov.get_secret_bundle("dep_a").secrets_epoch == 0


def test_release_gate_module_set_is_not_editable_here():
    settings, deployment, prov, control = _stores(selected=(), is_release_gate=True)
    with pytest.raises(ValueError, match="development gate"):
        set_deployment_modules(
            deployment=deployment, desired_module_ids=["buchhaltung"],
            provision_store=prov, control_store=control, settings=settings,
        )


def test_decommissioned_deployment_is_rejected():
    settings, deployment, prov, control = _stores(selected=(), removed_at="2026-07-24T00:00:00Z")
    with pytest.raises(ValueError, match="decommissioned"):
        set_deployment_modules(
            deployment=deployment, desired_module_ids=["buchhaltung"],
            provision_store=prov, control_store=control, settings=settings,
        )


def test_bundle_without_descriptor_is_rejected():
    settings, deployment, prov, control = _stores(selected=(), seed_descriptor=False)
    with pytest.raises(ValueError, match="descriptor"):
        set_deployment_modules(
            deployment=deployment, desired_module_ids=["buchhaltung"],
            provision_store=prov, control_store=control, settings=settings,
        )


# --- operator endpoint -------------------------------------------------------

def _admin_principal() -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id="admin@onebrain",
        role_id="admin",
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
    )


def _operator_settings():
    return SimpleNamespace(
        is_operator_surface=True,
        operator_mode=True,   # Mission Control: _authorize_deployment bypasses account authz
        secret_encryption_key="test-module-activation-key",
        secret_encryption_key_version="v1",
        bootstrap_secret_ttl_seconds=3600,
    )


def _endpoint_env(monkeypatch, *, selected=()):
    _settings_unused, deployment, prov, control = _stores(selected=selected)
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=deployment.account_id, kind="organization", name="Acme",
        owner_user_id="admin@onebrain"))
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(operator_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(operator_router, "get_settings", _operator_settings)
    return deployment, prov, control, platform


def test_endpoint_activates_db_only_module_and_records_audit(monkeypatch):
    deployment, prov, control, platform = _endpoint_env(monkeypatch, selected=())

    out = operator_router.activate_product_modules(
        "dep_a",
        operator_router.ProductModulesActivateIn(add_module_ids=["buchhaltung"]),
        principal=_admin_principal(),
    )

    assert out.changed is True
    assert out.added_module_ids == ["buchhaltung"]
    assert out.selected_module_ids == ["buchhaltung"]
    assert out.secrets_epoch == 1
    assert control.get_deployment("dep_a").selected_module_ids == ("buchhaltung",)
    events = platform.list_audit(deployment.account_id)
    assert any(e.action == "deployment.product_modules_activated" for e in events)


def test_endpoint_unions_adds_with_current_modules(monkeypatch):
    _endpoint_env(monkeypatch, selected=("kpi_dashboard",))
    out = operator_router.activate_product_modules(
        "dep_a",
        operator_router.ProductModulesActivateIn(add_module_ids=["buchhaltung"]),
        principal=_admin_principal(),
    )
    assert set(out.selected_module_ids) == {"kpi_dashboard", "buchhaltung"}
    assert out.added_module_ids == ["buchhaltung"]


def test_endpoint_maps_service_backed_rejection_to_409(monkeypatch):
    _endpoint_env(monkeypatch, selected=())
    with pytest.raises(HTTPException) as exc:
        operator_router.activate_product_modules(
            "dep_a",
            operator_router.ProductModulesActivateIn(add_module_ids=["communication"]),
            principal=_admin_principal(),
        )
    assert exc.value.status_code == 409


def test_endpoint_unknown_deployment_is_404(monkeypatch):
    _endpoint_env(monkeypatch, selected=())
    with pytest.raises(HTTPException) as exc:
        operator_router.activate_product_modules(
            "nope",
            operator_router.ProductModulesActivateIn(add_module_ids=["buchhaltung"]),
            principal=_admin_principal(),
        )
    assert exc.value.status_code == 404

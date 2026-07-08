"""Customer provisioning bundles for modular OneBrain rollouts."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.provisioning as provisioning_router
from app.assistant.contracts import ASSISTANT_PURPOSES
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment
from app.controlplane.memory import MemoryControlPlaneStore
from app.platform.memory import MemoryPlatformStore
from app.provisioning.service import CustomerProvisioner
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE, parse_key, verify_secret
from app.servicekeys.memory import MemoryServiceKeyStore


def _principal(role_id: str = "admin") -> Principal:
    role = ROLES[role_id]
    locations = None if role.scope == "chain" else frozenset({"munich"})
    return Principal(
        user_id=f"{role_id}@onebrain",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=locations,
        categories=role.categories,
        location_label="munich",
    )


def _stores():
    return MemoryPlatformStore(), MemoryControlPlaneStore()


def test_full_stack_provisioning_separates_private_and_customer_service_spaces():
    platform, control = _stores()

    result = CustomerProvisioner(platform, control).provision(
        account_id="acme",
        account_kind="organization",
        customer_name="Acme GmbH",
        owner_user_id="admin@onebrain",
        bundle_id="full_stack",
        deployment_id="dep_acme",
        deployment_type="dedicated_railway",
        region="eu-central",
        release_ring="pilot",
        initial_version="2026.07.0",
        module_versions={"assistant-service": "2026.07.assistant"},
    )

    assert result.account.id == "acme"
    assert {s.kind for s in result.spaces} == {"personal", "business", "customer_service", "shared", "family"}
    assert {m.module_id for m in result.modules} == {
        "onebrain-api",
        "onebrain-admin-ui",
        "onebrain-workers",
        "assistant-service",
        "communication-api",
        "communication-widget",
        "communication-voice",
        "communication-workers",
    }
    assert {m.module_id: m.version for m in result.modules}["assistant-service"] == "2026.07.assistant"

    communication = next(app for app in result.installations if app.app_id == "communication")
    enabled_kinds = {platform.get_space(space_id).kind for space_id in communication.enabled_space_ids}
    assert enabled_kinds == {"customer_service", "shared"}

    customer_space = next(s for s in result.spaces if s.kind == "customer_service")
    personal_space = next(s for s in result.spaces if s.kind == "personal")
    assert platform.check_app_access("acme", "communication", customer_space.id, "customer_service_answer").allowed
    private_decision = platform.check_app_access("acme", "communication", personal_space.id, "customer_service_answer")
    assert private_decision.allowed is False
    assert private_decision.reason == "customer_service_cannot_use_private_space"

    deployment = control.get_deployment("dep_acme")
    assert deployment and deployment.current_version == "2026.07.0"
    assert platform.list_audit("acme")[-1].action == "customer.provisioned"


def test_communication_bundle_does_not_install_assistant_modules():
    platform, control = _stores()

    CustomerProvisioner(platform, control).provision(
        account_id="supportco",
        account_kind="organization",
        customer_name="SupportCo",
        owner_user_id="admin@onebrain",
        bundle_id="onebrain_communication",
        deployment_id="dep_supportco",
        deployment_type="shared_railway",
        region="eu-central",
        release_ring="manual",
        initial_version="0.1.0",
    )

    module_ids = {m.module_id for m in control.list_modules("dep_supportco")}
    assert "assistant-service" not in module_ids
    assert {"communication-api", "communication-widget", "communication-voice", "communication-workers"} <= module_ids
    assert {s.kind for s in platform.list_spaces("supportco")} == {"business", "customer_service", "shared"}


def test_full_stack_provisioning_mints_constrained_integration_keys():
    platform, control = _stores()
    service_keys = MemoryServiceKeyStore()

    result = CustomerProvisioner(platform, control, service_keys).provision(
        account_id="acme",
        account_kind="organization",
        customer_name="Acme GmbH",
        owner_user_id="admin@onebrain",
        bundle_id="full_stack",
        deployment_id="dep_acme",
        deployment_type="dedicated_railway",
        region="eu-central",
        release_ring="pilot",
        initial_version="2026.07.0",
        mint_integration_keys=True,
    )

    assert {credential.app_id for credential in result.credentials} == {"assistant", "communication"}

    assistant = next(credential for credential in result.credentials if credential.app_id == "assistant")
    stored_assistant = service_keys.get(assistant.id)
    assert stored_assistant.account_id == "acme"
    assert stored_assistant.app_id == "assistant"
    assert set(stored_assistant.scopes) == {SCOPE_READ, SCOPE_WRITE}
    assert set(stored_assistant.purposes) == set(ASSISTANT_PURPOSES)

    communication = next(credential for credential in result.credentials if credential.app_id == "communication")
    stored = service_keys.get(communication.id)
    parsed = parse_key(communication.key)
    assert parsed is not None
    _, secret = parsed
    assert verify_secret(secret, stored.key_hash)
    assert stored.tenant_id == "acme"
    assert stored.account_id == "acme"
    assert stored.app_id == "communication"
    assert set(stored.scopes) == {SCOPE_READ, SCOPE_WRITE}
    assert set(stored.purposes) == {"customer_service_answer", "customer_service_inbox"}
    assert {platform.get_space(space_id).kind for space_id in stored.space_ids} == {"customer_service", "shared"}
    assert communication.key not in str(platform.list_audit("acme")[-1].meta)
    assert communication.id in platform.list_audit("acme")[-1].meta["service_key_ids"]


def test_duplicate_deployment_blocks_before_platform_records_are_created():
    platform, control = _stores()
    control.create_deployment(CustomerDeployment(id="dep_acme", customer_name="Existing"))

    with pytest.raises(ValueError, match="deployment already exists"):
        CustomerProvisioner(platform, control).provision(
            account_id="acme",
            account_kind="organization",
            customer_name="Acme",
            owner_user_id="admin@onebrain",
            bundle_id="full_stack",
            deployment_id="dep_acme",
            deployment_type="dedicated_railway",
            region="",
            release_ring="manual",
            initial_version="0.1.0",
        )

    assert platform.list_accounts() == []


def test_unknown_module_version_override_is_rejected_before_writes():
    platform, control = _stores()

    with pytest.raises(ValueError, match="Unknown module versions"):
        CustomerProvisioner(platform, control).provision(
            account_id="assistantco",
            account_kind="organization",
            customer_name="AssistantCo",
            owner_user_id="admin@onebrain",
            bundle_id="onebrain_assistant",
            deployment_id="dep_assistantco",
            deployment_type="dedicated_railway",
            region="",
            release_ring="manual",
            initial_version="0.1.0",
            module_versions={"communication-api": "0.2.0"},
        )

    assert platform.list_accounts() == []
    assert control.get_deployment("dep_assistantco") is None


def test_provisioning_router_requires_admin(monkeypatch):
    platform, control = _stores()
    service_keys = MemoryServiceKeyStore()
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_service_key_store", lambda: service_keys)

    with pytest.raises(HTTPException) as exc:
        provisioning_router.provision_customer(
            provisioning_router.CustomerProvisionCreate(
                customer_name="Acme",
                account_id="acme",
                deployment_id="dep_acme",
            ),
            principal=_principal("front_desk"),
        )
    assert exc.value.status_code == 403

    created = provisioning_router.provision_customer(
        provisioning_router.CustomerProvisionCreate(
            customer_name="Acme",
            bundle_id="onebrain_only",
            account_id="acme",
            deployment_id="dep_acme",
            initial_version="0.1.0",
        ),
        principal=_principal("admin"),
    )

    assert created.account.id == "acme"
    assert created.bundle_id == "onebrain_only"
    assert [app.app_id for app in created.apps] == ["onebrain_core"]
    assert created.credentials == []

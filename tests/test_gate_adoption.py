from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.controlplane.base import CustomerDeployment
from app.controlplane.memory import MemoryControlPlaneStore
from app.fleet.base import FleetKey, Heartbeat
from app.fleet.memory import MemoryFleetStore
from app.provisioning.gate_adoption import prepare_existing_gate_bundle
from app.provisioning.runs import BoxSecretBundle, MemoryProvisioningRunStore, OneTimeSecretCipher
from app.routers import operator as operator_router
from app.servicekeys.base import ServiceKey, generate_key, hash_secret
from app.servicekeys.memory import MemoryServiceKeyStore


def _settings():
    return SimpleNamespace(
        secret_encryption_key="test-gate-adoption-key",
        secret_encryption_key_version="v1",
        bootstrap_secret_ttl_seconds=3600,
        is_operator_surface=True,
        operator_mode=True,
        fleet_report_seconds=60,
        fleet_desired_state_private_key="",
        fleet_desired_state_public_keys="",
        fleet_desired_state_public_key="",
    )


def _legacy_bundle():
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
        "UPDATE_BACKUP_KEY": "b" * 32,
        "UPDATE_DESIRED_STATE_PUBLIC_KEYS": "",
        "ONEBRAIN_DNS_TOKEN": "",
        "ONEBRAIN_BACKUP_S3_ACCESS_KEY": "",
        "ONEBRAIN_BACKUP_S3_SECRET_KEY": "",
    }


def _stores():
    settings = _settings()
    deployment = CustomerDeployment(
        id="gate",
        customer_name="Existing gate",
        account_id="gate-account",
        environment="development",
        deployment_type="dedicated_server",
    )
    prov = MemoryProvisioningRunStore()
    prov.upsert_secret_bundle(BoxSecretBundle(
        deployment_id=deployment.id,
        account_id=deployment.account_id,
        ciphertext=OneTimeSecretCipher(settings).seal_bundle(json.dumps(_legacy_bundle())),
    ))
    return settings, deployment, prov, MemoryServiceKeyStore()


def test_prepare_existing_gate_bundle_is_secret_safe_and_idempotent():
    settings, deployment, prov, keys = _stores()

    first = prepare_existing_gate_bundle(
        deployment=deployment,
        provision_store=prov,
        service_key_store=keys,
        settings=settings,
        optional_module_ids=operator_router.DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS,
    )
    assert first.updated is True
    assert first.secrets_epoch == 1
    assert set(vars(first)) == {"deployment_id", "updated", "secrets_epoch"}
    stored_keys = keys.list_by_tenant("gate-account")
    assert {key.app_id for key in stored_keys} == {"assistant", "communication"}
    assert all(key.status == "active" for key in stored_keys)

    bundle = json.loads(OneTimeSecretCipher(settings).open_bundle(
        prov.get_secret_bundle(deployment.id).ciphertext
    ))
    assert bundle["ONEBRAIN_ASSISTANT_SERVICE_KEY"].startswith("sk_")
    assert bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"].startswith("sk_")
    assert bundle["ONEBRAIN_ASSISTANT_SERVICE_KEY"] != bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"]
    assert bundle["ONEBRAIN_COMMUNICATION_SPACE_ID"] == "sp_gate-account_customer_service"
    assert bundle["ONEBRAIN_SERVICE_KEY"] == bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"]

    second = prepare_existing_gate_bundle(
        deployment=deployment,
        provision_store=prov,
        service_key_store=keys,
        settings=settings,
        optional_module_ids=operator_router.DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS,
    )
    assert second.updated is False
    assert second.secrets_epoch == 1
    assert len(keys.list_by_tenant("gate-account")) == 2


def test_prepare_existing_gate_endpoint_waits_for_epoch_convergence(monkeypatch):
    settings, deployment, prov, keys = _stores()
    settings.fleet_report_seconds = None
    stale_id, stale_secret, stale_raw = generate_key()
    keys.create(ServiceKey(
        id=stale_id,
        key_hash=hash_secret(stale_secret),
        tenant_id=deployment.account_id,
        account_id=deployment.account_id,
        app_id="assistant",
        scopes=(),
    ))
    cipher = OneTimeSecretCipher(settings)
    bundle_row = prov.get_secret_bundle(deployment.id)
    bundle = json.loads(cipher.open_bundle(bundle_row.ciphertext))
    bundle["ONEBRAIN_ASSISTANT_SERVICE_KEY"] = stale_raw
    prov.upsert_secret_bundle(replace(
        bundle_row,
        ciphertext=cipher.seal_bundle(json.dumps(bundle)),
    ))
    control = MemoryControlPlaneStore()
    control.create_deployment(deployment)
    control.designate_release_gate(deployment.id)
    fleet = MemoryFleetStore()
    fleet.create_key(FleetKey("fleet-key", "hash", deployment.id))
    timestamp = datetime.now(timezone.utc).isoformat()
    fleet.record_heartbeat(Heartbeat(
        id="hb-1",
        deployment_id=deployment.id,
        contract_version="fleet.v2",
        reported_at=timestamp,
        received_at=timestamp,
        healthy=True,
        payload={"update": {"applied_secrets_epoch": 0}},
    ))
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(operator_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(operator_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(operator_router, "get_service_key_store", lambda: keys)
    principal = SimpleNamespace(role_id="admin", user_id="operator")

    first = operator_router.prepare_existing_development_gate(principal)
    assert first.updated is True
    assert first.secrets_epoch == 1
    assert first.applied_secrets_epoch == 0
    assert first.ready is False
    assert first.blockers == ["secrets_epoch_pending"]
    assert keys.get(stale_id).status == "active"

    timestamp = datetime.now(timezone.utc).isoformat()
    fleet.record_heartbeat(Heartbeat(
        id="hb-2",
        deployment_id=deployment.id,
        contract_version="fleet.v2",
        reported_at=timestamp,
        received_at=timestamp,
        healthy=True,
        payload={"update": {"applied_secrets_epoch": 1}},
    ))
    second = operator_router.prepare_existing_development_gate(principal)
    assert second.updated is False
    assert second.ready is True
    assert second.blockers == []
    assert keys.get(stale_id).status == "revoked"


def test_prepare_existing_gate_revokes_new_keys_when_bundle_write_fails():
    settings, deployment, original, keys = _stores()

    class FailingProvisionStore:
        def get_secret_bundle(self, deployment_id):
            return original.get_secret_bundle(deployment_id)

        def upsert_secret_bundle(self, _bundle):
            raise RuntimeError("simulated storage failure")

    before = original.get_secret_bundle(deployment.id).ciphertext
    with pytest.raises(RuntimeError, match="simulated storage failure"):
        prepare_existing_gate_bundle(
            deployment=deployment,
            provision_store=FailingProvisionStore(),
            service_key_store=keys,
            settings=settings,
            optional_module_ids=operator_router.DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS,
        )

    assert original.get_secret_bundle(deployment.id).ciphertext == before
    created = keys.list_by_tenant("gate-account")
    assert len(created) == 2
    assert all(key.status == "revoked" for key in created)


def test_prepare_existing_gate_endpoint_is_admin_only(monkeypatch):
    monkeypatch.setattr(operator_router, "get_settings", _settings)
    with pytest.raises(HTTPException) as exc:
        operator_router.prepare_existing_development_gate(
            SimpleNamespace(role_id="front_desk", user_id="person")
        )
    assert exc.value.status_code == 403

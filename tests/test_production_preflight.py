"""Fail-closed production Mission Control configuration checks."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings
from app.controlplane.base import CustomerDeployment
from app.trust.signing import generate_keypair


def _production_mc_settings(tmp_path, **overrides) -> Settings:
    desired_state_private, desired_state_public = generate_keypair()
    _release_private, release_public = generate_keypair()
    cert = tmp_path / "mc-client.crt"
    key = tmp_path / "mc-client.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("private-key", encoding="utf-8")
    values = {
        "environment": "production",
        "operator_mode": True,
        "vector_store": "pgvector",
        "database_url": "postgresql://onebrain:secret@postgres/onebrain",
        "operator_database_url": "postgresql://onebrain_owner:secret@postgres/onebrain",
        "rls_enforced": True,
        "secret_encryption_key": Fernet.generate_key().decode("ascii"),
        "postgres_app_role": "onebrain_app",
        "postgres_worker_role": "onebrain_worker",
        "login_rate_limit_secret": "login-limit-secret-for-production-tests",
        "provisioning_callback_allowed_hosts": "mc.example",
        "provisioner_backend": "hetzner",
        "hetzner_broker_url": "https://broker.example",
        "hetzner_broker_credential": "mc-broker-credential",
        "hetzner_broker_client_certificate_file": str(cert),
        "hetzner_broker_client_key_file": str(key),
        "fleet_url": "https://mc.example",
        "fleet_public_url": "https://mc.example",
        "fleet_key": "fk_test",
        "deployment_id": "mc",
        "fleet_desired_state_private_key": desired_state_private,
        "fleet_desired_state_public_keys": desired_state_public,
        "fleet_desired_state_ttl_seconds": 900,
        "release_verify_public_key": release_public,
        "release_require_signature": True,
        "release_require_signed_images": True,
        "release_require_rollback_kind": True,
        "release_promotion_required": True,
        "fleet_reconcile_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


def test_provisioner_and_customer_defaults_fail_closed():
    assert Settings().provisioner_backend == "disabled"
    assert CustomerDeployment(id="customer", customer_name="Customer").deployment_type == "dedicated_server"


def test_provisioner_backend_allows_only_disabled_or_hetzner():
    with pytest.raises(ValidationError, match="provisioner_backend"):
        Settings(provisioner_backend="github")


def test_complete_production_mission_control_configuration_passes_preflight(tmp_path):
    _production_mc_settings(tmp_path).assert_production_mission_control_ready()


def test_development_mission_control_keeps_the_testable_defaults():
    # The strict production preflight must not make local/dev tests supply a
    # broker, signing keys, or a production database.
    Settings(environment="development", operator_mode=True).assert_production_mission_control_ready()


def test_production_operator_console_cannot_bypass_control_plane_preflight(tmp_path):
    settings = _production_mc_settings(tmp_path, operator_mode=False, operator_console=True)

    settings.assert_production_mission_control_ready()

    incomplete = Settings(
        environment="production",
        operator_console=True,
        vector_store="pgvector",
        database_url="postgresql://onebrain:secret@postgres/onebrain",
        rls_enforced=True,
        login_rate_limit_secret="login-limit-secret-for-production-tests",
    )
    with pytest.raises(RuntimeError, match="ONEBRAIN_PROVISIONER_BACKEND=hetzner"):
        incomplete.assert_production_mission_control_ready()


@pytest.mark.parametrize(
    ("override", "marker"),
    [
        ({"provisioner_backend": "disabled"}, "ONEBRAIN_PROVISIONER_BACKEND=hetzner"),
        ({"hetzner_broker_url": "http://broker.example"}, "ONEBRAIN_HETZNER_BROKER_URL"),
        ({"hetzner_broker_client_key_file": ""}, "ONEBRAIN_HETZNER_BROKER_CLIENT_KEY_FILE"),
        ({"fleet_url": "http://mc.example"}, "ONEBRAIN_FLEET_URL"),
        ({"fleet_public_url": "http://mc.example"}, "ONEBRAIN_FLEET_PUBLIC_URL"),
        ({"fleet_desired_state_private_key": ""}, "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY"),
        ({"release_verify_public_key": ""}, "ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY"),
        ({"release_require_signature": False}, "ONEBRAIN_RELEASE_REQUIRE_SIGNATURE=true"),
        ({"release_require_signed_images": False}, "ONEBRAIN_RELEASE_REQUIRE_SIGNED_IMAGES=true"),
        ({"release_require_rollback_kind": False}, "ONEBRAIN_RELEASE_REQUIRE_ROLLBACK_KIND=true"),
        ({"release_promotion_required": False}, "ONEBRAIN_RELEASE_PROMOTION_REQUIRED=true"),
        ({"rls_enforced": False}, "ONEBRAIN_RLS_ENFORCED=true"),
        ({"operator_database_url": ""}, "ONEBRAIN_OPERATOR_DATABASE_URL"),
        (
            {
                "operator_database_url": (
                    "postgresql://onebrain:other-secret@postgres:5432/onebrain/"
                )
            },
            "ONEBRAIN_OPERATOR_DATABASE_URL must use a distinct PostgreSQL login role",
        ),
        ({"secret_encryption_key": ""}, "ONEBRAIN_SECRET_ENCRYPTION_KEY is required"),
        ({"secret_encryption_key": "not-a-fernet-key"}, "ONEBRAIN_SECRET_ENCRYPTION_KEY must be a URL-safe base64 Fernet key"),
        ({"postgres_app_role": ""}, "ONEBRAIN_POSTGRES_APP_ROLE"),
        ({"postgres_worker_role": ""}, "ONEBRAIN_POSTGRES_WORKER_ROLE"),
        ({"login_rate_limit_secret": ""}, "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"),
        ({"provisioning_callback_allowed_hosts": ""}, "ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS"),
        ({"fleet_reconcile_seconds": 0}, "ONEBRAIN_FLEET_RECONCILE_SECONDS"),
    ],
)
def test_production_mission_control_preflight_rejects_missing_security_guards(tmp_path, override, marker):
    settings = _production_mc_settings(tmp_path, **override)

    with pytest.raises(RuntimeError, match=marker):
        settings.assert_production_mission_control_ready()


def test_production_mission_control_preflight_rejects_unserved_signer(tmp_path):
    _other_private, unexpected_public = generate_keypair()
    settings = _production_mc_settings(tmp_path, fleet_desired_state_public_keys=unexpected_public)

    with pytest.raises(RuntimeError, match="active desired-state signer"):
        settings.assert_production_mission_control_ready()


def test_production_mission_control_preflight_rejects_inprocess_or_direct_cloud_access(tmp_path):
    settings = _production_mc_settings(
        tmp_path,
        hetzner_allow_inprocess_broker=True,
        hetzner_api_token="hetzner-cloud-token",
    )

    with pytest.raises(RuntimeError) as exc:
        settings.assert_production_mission_control_ready()
    assert "in-process" in str(exc.value)
    assert "ONEBRAIN_HETZNER_API_TOKEN must be empty" in str(exc.value)

from __future__ import annotations

from dataclasses import replace

import pytest

from app.auth.passwords import hash_password
from app.platform.memory import MemoryPlatformStore
from app.provisioning.customer_bootstrap import (
    CustomerBootstrapDescriptor,
    decode_customer_bootstrap,
    encode_customer_bootstrap,
    reconcile_customer_bootstrap,
)
from app.servicekeys.base import generate_key
from app.servicekeys.memory import MemoryServiceKeyStore
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore
from app.users.base import User
from app.users.memory import MemoryUserStore


def _descriptor() -> CustomerBootstrapDescriptor:
    return CustomerBootstrapDescriptor(
        account_id="onebrain-development",
        account_kind="project",
        customer_name="One Brain Development Gate",
        bundle_id="full_stack",
    )


def _integration_keys() -> dict[str, str]:
    return {
        "assistant": generate_key()[2],
        "communication": generate_key()[2],
    }


def test_customer_bootstrap_descriptor_round_trips_and_is_deterministic():
    descriptor = _descriptor()

    encoded = encode_customer_bootstrap(descriptor)

    assert encoded == encode_customer_bootstrap(descriptor)
    assert decode_customer_bootstrap(encoded) == descriptor
    assert decode_customer_bootstrap("") is None


@pytest.mark.parametrize(
    "encoded,match",
    [
        ("not-base64!", "base64"),
        ("e30", "fields"),
        ("eHh4", "JSON"),
        ("x" * 5000, "too large"),
    ],
)
def test_customer_bootstrap_descriptor_rejects_invalid_payloads(encoded: str, match: str):
    with pytest.raises(ValueError, match=match):
        decode_customer_bootstrap(encoded)


def test_customer_bootstrap_descriptor_rejects_unknown_bundle_and_unsafe_identity():
    with pytest.raises(ValueError, match="bundle"):
        encode_customer_bootstrap(replace(_descriptor(), bundle_id="not_a_bundle"))

    with pytest.raises(ValueError, match="account id"):
        encode_customer_bootstrap(replace(_descriptor(), account_id="bad\naccount"))


def test_full_stack_bootstrap_creates_local_topology_credentials_and_audit_once():
    platform = MemoryPlatformStore()
    service_keys = MemoryServiceKeyStore()
    users = MemoryUserStore()
    sessions = MemorySessionStore()
    admin = users.create(User(
        id="usr_admin",
        email="owner@example.test",
        display_name="Administrator",
        password_hash=hash_password("correct horse battery staple"),
        tenant_id="onebrain-development",
        role_id="admin",
        location="all",
    ))
    raw_keys = _integration_keys()

    first = reconcile_customer_bootstrap(
        _descriptor(),
        platform_store=platform,
        service_key_store=service_keys,
        user_store=users,
        session_store=sessions,
        administrator_email=admin.email,
        integration_keys=raw_keys,
    )
    second = reconcile_customer_bootstrap(
        _descriptor(),
        platform_store=platform,
        service_key_store=service_keys,
        user_store=users,
        session_store=sessions,
        administrator_email=admin.email,
        integration_keys=raw_keys,
    )

    assert first.account_id == "onebrain-development"
    assert second.account_id == first.account_id
    assert len(platform.list_accounts()) == 1
    assert {space.kind for space in platform.list_spaces(first.account_id)} == {
        "personal", "business", "customer_service", "shared", "family",
    }
    installations = platform.list_app_installations(first.account_id)
    assert {installation.app_id for installation in installations} == {
        "onebrain_core", "assistant", "communication", "kpi_dashboard", "ai_employees",
    }
    assert len(platform.list_audit(first.account_id)) == 1
    assert platform.list_audit(first.account_id)[0].action == "customer.bootstrap_reconciled"
    assert platform.get_brand_theme(first.account_id) is not None

    keys = service_keys.list_by_tenant(first.account_id)
    assert {key.app_id for key in keys} == {"assistant", "communication"}
    assistant = next(key for key in keys if key.app_id == "assistant")
    communication = next(key for key in keys if key.app_id == "communication")
    assert "assistant_context" in assistant.purposes
    assert not any(purpose.startswith("customer_service") for purpose in assistant.purposes)
    assert set(communication.purposes) == {"customer_service_answer", "customer_service_inbox"}
    assert {platform.get_space(space_id).kind for space_id in communication.space_ids} == {
        "customer_service", "shared",
    }


def test_bootstrap_repairs_only_configured_legacy_admin_and_revokes_its_sessions():
    platform = MemoryPlatformStore()
    service_keys = MemoryServiceKeyStore()
    users = MemoryUserStore()
    sessions = MemorySessionStore()
    password_hash = hash_password("correct horse battery staple")
    admin = users.create(User(
        id="usr_admin",
        email="owner@example.test",
        display_name="Administrator",
        password_hash=password_hash,
        tenant_id="nft_gym",
        role_id="admin",
        location="all",
        must_change_password=True,
    ))
    other = users.create(User(
        id="usr_other",
        email="other@example.test",
        display_name="Other admin",
        password_hash=password_hash,
        tenant_id="nft_gym",
        role_id="admin",
        location="all",
    ))
    sessions.create(Session(
        id="sess_admin",
        user_id=admin.id,
        tenant_id="nft_gym",
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-18T00:00:00+00:00",
    ))
    sessions.create(Session(
        id="sess_other",
        user_id=other.id,
        tenant_id="nft_gym",
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-18T00:00:00+00:00",
    ))

    result = reconcile_customer_bootstrap(
        _descriptor(),
        platform_store=platform,
        service_key_store=service_keys,
        user_store=users,
        session_store=sessions,
        administrator_email=admin.email,
        integration_keys=_integration_keys(),
    )

    repaired = users.get(admin.id)
    untouched = users.get(other.id)
    assert result.administrator_rebound is True
    assert repaired is not None
    assert repaired.tenant_id == "onebrain-development"
    assert repaired.password_hash == password_hash
    assert repaired.must_change_password is True
    assert untouched is not None and untouched.tenant_id == "nft_gym"
    assert sessions.get("sess_admin").revoked_at
    assert not sessions.get("sess_other").revoked_at


def test_full_stack_bootstrap_requires_distinct_assistant_and_communication_keys():
    shared = generate_key()[2]

    with pytest.raises(ValueError, match="distinct"):
        reconcile_customer_bootstrap(
            _descriptor(),
            platform_store=MemoryPlatformStore(),
            service_key_store=MemoryServiceKeyStore(),
            user_store=MemoryUserStore(),
            session_store=MemorySessionStore(),
            administrator_email="owner@example.test",
            integration_keys={"assistant": shared, "communication": shared},
        )

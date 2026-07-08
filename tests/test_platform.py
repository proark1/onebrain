"""Platform foundation: spaces, app purposes, and audit boundaries."""

from __future__ import annotations

import pytest

from app.platform.base import Account, AppInstallation, AuditEvent, BrandTheme, Space
from app.platform.memory import MemoryPlatformStore
from app.security.policy import AccessFilter, Classification


def _seed_platform() -> MemoryPlatformStore:
    store = MemoryPlatformStore()
    store.create_account(Account(id="acct_owner", kind="organization", name="Owner GmbH", owner_user_id="u_admin"))
    store.create_space(Space(id="sp_personal", account_id="acct_owner", kind="personal", name="Personal"))
    store.create_space(Space(id="sp_business", account_id="acct_owner", kind="business", name="Business"))
    store.create_space(Space(id="sp_customer", account_id="acct_owner", kind="customer_service", name="Customer service"))
    store.create_space(Space(id="sp_shared", account_id="acct_owner", kind="shared", name="Owner shared"))
    return store


def test_customer_service_purpose_cannot_use_private_spaces_even_if_enabled():
    store = _seed_platform()
    store.install_app(AppInstallation(
        id="appi_comm",
        account_id="acct_owner",
        app_id="communication",
        enabled_space_ids=("sp_personal", "sp_customer", "sp_shared"),
        allowed_purposes=("customer_service_answer",),
    ))

    private_decision = store.check_app_access(
        "acct_owner", "communication", "sp_personal", "customer_service_answer",
    )
    assert private_decision.allowed is False
    assert private_decision.reason == "customer_service_cannot_use_private_space"

    customer_decision = store.check_app_access(
        "acct_owner", "communication", "sp_customer", "customer_service_answer",
    )
    assert customer_decision.allowed is True

    shared_decision = store.check_app_access(
        "acct_owner", "communication", "sp_shared", "customer_service_answer",
    )
    assert shared_decision.allowed is True


def test_app_access_requires_explicit_space_and_purpose():
    store = _seed_platform()
    store.install_app(AppInstallation(
        id="appi_assistant",
        account_id="acct_owner",
        app_id="assistant",
        enabled_space_ids=("sp_personal",),
        allowed_purposes=("assistant_context",),
    ))

    assert store.check_app_access("acct_owner", "assistant", "sp_personal", "assistant_context").allowed is True
    not_enabled = store.check_app_access("acct_owner", "assistant", "sp_business", "assistant_context")
    assert not_enabled.allowed is False
    assert not_enabled.reason == "purpose_or_space_not_enabled"

    wrong_purpose = store.check_app_access("acct_owner", "assistant", "sp_personal", "customer_service_answer")
    assert wrong_purpose.allowed is False


def test_installation_rejects_cross_account_spaces_and_unknown_purposes():
    store = _seed_platform()
    store.create_account(Account(id="acct_other", kind="organization", name="Other GmbH"))
    store.create_space(Space(id="sp_other", account_id="acct_other", kind="business", name="Other business"))

    with pytest.raises(ValueError, match="space is not in this account"):
        store.install_app(AppInstallation(
            id="appi_bad_space",
            account_id="acct_owner",
            app_id="assistant",
            enabled_space_ids=("sp_other",),
            allowed_purposes=("assistant_context",),
        ))

    with pytest.raises(ValueError, match="Unknown purposes"):
        store.install_app(AppInstallation(
            id="appi_bad_purpose",
            account_id="acct_owner",
            app_id="assistant",
            enabled_space_ids=("sp_personal",),
            allowed_purposes=("read_everything",),
        ))


def test_audit_events_are_account_scoped():
    store = _seed_platform()
    event = store.record_audit(AuditEvent(
        id="aud_1",
        account_id="acct_owner",
        actor_id="u_admin",
        actor_type="human",
        action="access.checked",
        target_type="space",
        target_id="sp_customer",
        space_id="sp_customer",
        app_id="communication",
        purpose="customer_service_answer",
        decision="allowed",
        meta={"reason": "allowed"},
    ))
    assert event.id == "aud_1"

    audit = store.list_audit("acct_owner")
    assert len(audit) == 1
    assert audit[0].app_id == "communication"
    assert audit[0].purpose == "customer_service_answer"
    assert store.list_audit("acct_other") == []


def test_brand_theme_resolves_account_default_and_app_override():
    store = _seed_platform()
    store.install_app(AppInstallation(
        id="appi_assistant",
        account_id="acct_owner",
        app_id="assistant",
        enabled_space_ids=("sp_personal",),
        allowed_purposes=("assistant_context",),
    ))

    account_theme = store.upsert_brand_theme(BrandTheme(
        id="brand_acct_owner_account",
        account_id="acct_owner",
        name="Owner",
        primary_color="#123456",
        secondary_color="#234567",
        accent_color="#345678",
        background_color="#f4f2ee",
        surface_color="#ffffff",
        text_color="#101828",
        muted_color="#5f6671",
        success_color="#1f7a4d",
        warning_color="#b98a4e",
        danger_color="#b4453e",
    ))
    assert account_theme.primary_color == "#123456"
    assert store.resolve_brand_theme("acct_owner", "communication").primary_color == "#123456"

    app_theme = store.upsert_brand_theme(BrandTheme(
        id="brand_acct_owner_assistant",
        account_id="acct_owner",
        app_id="assistant",
        name="Assistant",
        primary_color="#abcdef",
        secondary_color="#234567",
        accent_color="#345678",
        background_color="#f4f2ee",
        surface_color="#ffffff",
        text_color="#101828",
        muted_color="#5f6671",
        success_color="#1f7a4d",
        warning_color="#b98a4e",
        danger_color="#b4453e",
    ))
    assert app_theme.app_id == "assistant"
    assert store.resolve_brand_theme("acct_owner", "assistant").primary_color == "#abcdef"
    assert len(store.list_brand_themes("acct_owner")) == 2


def test_brand_theme_rejects_invalid_colors_and_uninstalled_app_override():
    store = _seed_platform()

    with pytest.raises(ValueError, match="Invalid hex color"):
        store.upsert_brand_theme(BrandTheme(
            id="brand_bad",
            account_id="acct_owner",
            primary_color="blue",
            secondary_color="#234567",
            accent_color="#345678",
            background_color="#f4f2ee",
            surface_color="#ffffff",
            text_color="#101828",
            muted_color="#5f6671",
            success_color="#1f7a4d",
            warning_color="#b98a4e",
            danger_color="#b4453e",
        ))

    with pytest.raises(ValueError, match="app is not installed"):
        store.upsert_brand_theme(BrandTheme(
            id="brand_app",
            account_id="acct_owner",
            app_id="assistant",
            primary_color="#123456",
            secondary_color="#234567",
            accent_color="#345678",
            background_color="#f4f2ee",
            surface_color="#ffffff",
            text_color="#101828",
            muted_color="#5f6671",
            success_color="#1f7a4d",
            warning_color="#b98a4e",
            danger_color="#b4453e",
        ))


def test_access_filter_can_narrow_chunks_to_one_platform_space():
    access = AccessFilter(
        "nft_gym",
        int(Classification.PUBLIC),
        frozenset(),
        frozenset({"general"}),
        account_id="acct_owner",
        space_ids=frozenset({"sp_customer"}),
    )
    base = {
        "tenant_id": "nft_gym",
        "classification": int(Classification.PUBLIC),
        "location": "global",
        "category": "general",
        "status": "approved",
    }

    assert access.allows({**base, "account_id": "acct_owner", "space_id": "sp_customer"}) is True
    assert access.allows({**base, "account_id": "acct_owner", "space_id": "sp_personal"}) is False
    assert access.allows({**base, "space_id": "sp_customer"}) is False
    assert access.allows({**base, "account_id": "acct_other", "space_id": "sp_customer"}) is False

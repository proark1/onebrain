"""Platform foundation: spaces, app purposes, and audit boundaries."""

from __future__ import annotations

import pytest

from app.platform.base import (
    Account,
    AppInstallation,
    AuditEvent,
    BrandTheme,
    ConsentRecord,
    CredentialMetadata,
    DataAccessEvent,
    Membership,
    Organization,
    ProcessorRegistration,
    ProviderRegistration,
    RetentionPolicy,
    Space,
)
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


def test_governance_records_are_account_and_space_scoped():
    store = _seed_platform()

    org = store.upsert_organization(Organization(id="org_1", account_id="acct_owner", name="Ops"))
    membership = store.upsert_membership(Membership(
        id="mem_1",
        account_id="acct_owner",
        user_id="u_admin",
        role_id="admin",
        space_id="sp_business",
        organization_id=org.id,
    ))
    consent = store.upsert_consent_record(ConsentRecord(
        id="cons_1",
        account_id="acct_owner",
        space_id="sp_business",
        subject_ref="customer:1",
        purpose="customer_service_answer",
        status="granted",
    ))
    retention = store.upsert_retention_policy(RetentionPolicy(
        id="ret_1",
        account_id="acct_owner",
        space_id="sp_business",
        domain="intake",
        record_type="message",
        action="delete",
        duration_days=30,
        legal_basis="customer request",
    ))
    access = store.record_data_access(DataAccessEvent(
        id="dae_1",
        account_id="acct_owner",
        space_id="sp_business",
        actor_id="svc",
        actor_type="service",
        action="read",
        target_type="intake_record",
        target_id="rec_1",
        decision="allowed",
    ))
    processor = store.upsert_processor(ProcessorRegistration(
        id="proc_1",
        account_id="acct_owner",
        name="Railway",
        category="hosting",
        region="EU",
        dpa_status="signed",
    ))
    provider = store.upsert_provider(ProviderRegistration(
        id="prov_1",
        name="Gemini",
        category="ai",
        region="EU",
        dpia_status="approved",
    ))
    credential = store.upsert_credential_metadata(CredentialMetadata(
        id="cred_1",
        account_id="acct_owner",
        provider="google",
        app_id="assistant",
        secret_ref="secret://assistant/google/oauth",
    ))

    assert store.list_organizations("acct_owner") == [org]
    assert store.list_memberships("acct_owner") == [membership]
    assert store.list_consent_records("acct_owner", "sp_business") == [consent]
    assert store.list_retention_policies("acct_owner", "sp_business") == [retention]
    assert store.list_data_access_events("acct_owner", "sp_business") == [access]
    assert store.list_processors("acct_owner") == [processor]
    assert store.list_providers("acct_owner") == [provider]
    assert store.list_credential_metadata("acct_owner") == [credential]
    assert "oauth" in credential.secret_ref
    assert "raw-secret" not in str(credential)


def test_governance_delete_by_scope_leaves_other_spaces_and_global_registers():
    store = _seed_platform()
    store.upsert_organization(Organization(id="org_1", account_id="acct_owner", name="Ops"))
    store.upsert_membership(Membership(id="mem_biz", account_id="acct_owner", user_id="u1", role_id="admin", space_id="sp_business"))
    store.upsert_membership(Membership(id="mem_customer", account_id="acct_owner", user_id="u2", role_id="admin", space_id="sp_customer"))
    store.upsert_consent_record(ConsentRecord(id="cons_biz", account_id="acct_owner", subject_ref="s", purpose="p", status="granted", space_id="sp_business"))
    store.upsert_consent_record(ConsentRecord(id="cons_customer", account_id="acct_owner", subject_ref="s", purpose="p", status="granted", space_id="sp_customer"))
    store.upsert_processor(ProcessorRegistration(id="proc_global", name="Railway", category="hosting", region="EU", dpa_status="signed"))

    deleted = store.delete_governance_by_scope("acct_owner", "sp_business")

    assert deleted["memberships"] == 1
    assert deleted["consent_records"] == 1
    assert store.list_organizations("acct_owner")[0].id == "org_1"
    assert [row.id for row in store.list_memberships("acct_owner")] == ["mem_customer"]
    assert [row.id for row in store.list_consent_records("acct_owner")] == ["cons_customer"]
    assert store.list_processors("acct_owner")[0].id == "proc_global"


def test_kpi_dashboard_app_can_read_configure_and_write_snapshots():
    store = _seed_platform()
    store.install_app(AppInstallation(
        id="appi_kpi",
        account_id="acct_owner",
        app_id="kpi_dashboard",
        enabled_space_ids=("sp_business", "sp_shared"),
        allowed_purposes=("kpi_read", "kpi_configure", "kpi_snapshot_write"),
    ))

    assert store.check_app_access("acct_owner", "kpi_dashboard", "sp_business", "kpi_read").allowed is True
    assert store.check_app_access("acct_owner", "kpi_dashboard", "sp_shared", "kpi_snapshot_write").allowed is True
    private_decision = store.check_app_access("acct_owner", "kpi_dashboard", "sp_personal", "kpi_read")
    assert private_decision.allowed is False
    assert private_decision.reason == "purpose_or_space_not_enabled"

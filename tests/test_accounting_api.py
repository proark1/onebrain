"""Accounting (Buchhaltung) module gate + overview authorization contracts.

Phase 0: the module is off by default. Its endpoints must 403 unless the
``buchhaltung`` app is installed for the space with the ``accounting_read``
purpose, and where it is installed the (empty) overview reads as zeros.
"""

import pytest
from fastapi import HTTPException

import app.routers.accounting as accounting_router
from app.accounting.memory import MemoryAccountingStore
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.platform.base import Account, AppInstallation, Space
from app.platform.memory import MemoryPlatformStore


def _human(role_id: str = "admin", user_id: str = "admin@acme", tenant_id: str = "acme") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=user_id,
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"berlin"}),
        categories=role.categories,
        location_label="all",
        tenant_id=tenant_id,
    )


def _stores(*, install: bool = True):
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id="acme", kind="organization", name="Acme", owner_user_id="admin@acme",
    ))
    platform.create_space(Space(
        id="sp_business", account_id="acme", kind="business", name="Business",
    ))
    platform.create_space(Space(
        id="sp_shared", account_id="acme", kind="shared", name="Shared",
    ))
    if install:
        platform.install_app(AppInstallation(
            id="appi_buchhaltung",
            account_id="acme",
            app_id="buchhaltung",
            enabled_space_ids=("sp_business",),
            allowed_purposes=(
                "accounting_read", "accounting_ingest",
                "accounting_configure", "accounting_export",
            ),
        ))
    return platform, MemoryAccountingStore()


def _wire(monkeypatch, *, install: bool = True):
    platform, accounting = _stores(install=install)
    monkeypatch.setattr(accounting_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(accounting_router, "get_accounting_store", lambda: accounting)
    return platform, accounting


def test_installed_workspace_lists_and_reads_empty_overview(monkeypatch):
    _wire(monkeypatch, install=True)
    workspaces = accounting_router.list_accounting_workspaces(principal=_human())
    assert [row.space_id for row in workspaces] == ["sp_business"]
    overview = accounting_router.get_accounting_overview(
        account_id="acme", space_id="sp_business", principal=_human(),
    )
    assert (
        overview.total_documents,
        overview.pending_documents,
        overview.confirmed_documents,
    ) == (0, 0, 0)


def test_gate_is_off_without_installation(monkeypatch):
    _wire(monkeypatch, install=False)
    # No install anywhere → no workspaces, and the overview is forbidden.
    assert accounting_router.list_accounting_workspaces(principal=_human()) == []
    with pytest.raises(HTTPException) as forbidden:
        accounting_router.get_accounting_overview(
            account_id="acme", space_id="sp_business", principal=_human(),
        )
    assert forbidden.value.status_code == 403


def test_overview_forbidden_on_a_space_without_the_app(monkeypatch):
    _wire(monkeypatch, install=True)
    # Installed on sp_business only → sp_shared must stay gated.
    with pytest.raises(HTTPException) as forbidden:
        accounting_router.get_accounting_overview(
            account_id="acme", space_id="sp_shared", principal=_human(),
        )
    assert forbidden.value.status_code in {403, 404}


def test_cross_tenant_ids_fail_closed(monkeypatch):
    _wire(monkeypatch, install=True)
    with pytest.raises(HTTPException) as cross:
        accounting_router.get_accounting_overview(
            account_id="acme",
            space_id="sp_business",
            principal=_human(user_id="admin@other", tenant_id="other"),
        )
    assert cross.value.status_code == 404

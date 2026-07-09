"""Account-scoped admin authorization.

Holding the `admin` role is not enough — an admin may only act on accounts they
own or hold an active admin membership in. This is the boundary that keeps an
admin of one account from reaching another account's governance data.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.platform as platform_router
from app.auth.account_access import (
    authorize_account_admin,
    authorized_account_ids,
    is_account_admin,
)
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.platform.base import Account, Membership
from app.platform.memory import MemoryPlatformStore


def _admin(user_id: str) -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id=user_id, role_id="admin", role_label=role.label, clearance=role.clearance,
        locations=None, categories=role.categories, location_label="all", tenant_id="nft_gym",
    )


def _non_admin(role_id: str) -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@x", role_id=role_id, role_label=role.label, clearance=role.clearance,
        locations=frozenset({"munich"}), categories=role.categories, location_label="munich",
        tenant_id="nft_gym",
    )


def _service() -> Principal:
    return Principal(
        user_id="svc:key", role_id="service", role_label="Service", clearance=ROLES["public"].clearance,
        locations=frozenset(), categories=frozenset({"general"}), location_label="—",
        tenant_id="nft_gym", principal_type="service",
    )


def _store() -> MemoryPlatformStore:
    store = MemoryPlatformStore()
    store.create_account(Account(id="acct_a", kind="organization", name="A", owner_user_id="owner_a"))
    store.create_account(Account(id="acct_b", kind="organization", name="B", owner_user_id="owner_b"))
    return store


def test_owner_is_authorized_for_their_account_only():
    store = _store()
    assert authorize_account_admin(_admin("owner_a"), "acct_a", store).id == "acct_a"
    with pytest.raises(HTTPException) as exc:
        authorize_account_admin(_admin("owner_a"), "acct_b", store)
    assert exc.value.status_code == 404


def test_missing_account_and_unauthorized_account_look_identical():
    store = _store()
    with pytest.raises(HTTPException) as missing:
        authorize_account_admin(_admin("owner_a"), "no_such_account", store)
    with pytest.raises(HTTPException) as unauth:
        authorize_account_admin(_admin("stranger"), "acct_a", store)
    assert missing.value.status_code == unauth.value.status_code == 404


def test_non_admin_role_is_forbidden():
    store = _store()
    with pytest.raises(HTTPException) as exc:
        authorize_account_admin(_non_admin("front_desk"), "acct_a", store)
    assert exc.value.status_code == 403


def test_service_principal_can_never_administer_accounts():
    store = _store()
    with pytest.raises(HTTPException) as exc:
        authorize_account_admin(_service(), "acct_a", store)
    assert exc.value.status_code == 403


def test_only_active_admin_membership_conveys_access():
    store = _store()
    store.upsert_membership(Membership(id="m_active", account_id="acct_a", user_id="ops@x", role_id="admin"))
    store.upsert_membership(Membership(id="m_revoked", account_id="acct_b", user_id="ops@x", role_id="admin", status="revoked"))
    store.upsert_membership(Membership(id="m_viewer", account_id="acct_b", user_id="viewer@x", role_id="viewer"))

    assert is_account_admin(_admin("ops@x"), store.get_account("acct_a"), store) is True   # active admin
    assert is_account_admin(_admin("ops@x"), store.get_account("acct_b"), store) is False  # revoked
    assert is_account_admin(_admin("viewer@x"), store.get_account("acct_b"), store) is False  # not an admin role


def test_authorized_account_ids_covers_owned_and_member_accounts():
    store = _store()
    store.upsert_membership(Membership(id="m1", account_id="acct_b", user_id="owner_a", role_id="admin"))
    assert authorized_account_ids(_admin("owner_a"), store) == {"acct_a", "acct_b"}
    assert authorized_account_ids(_admin("owner_b"), store) == {"acct_b"}
    assert authorized_account_ids(_admin("stranger"), store) == set()


def test_platform_routes_deny_cross_account_and_scope_listing(monkeypatch):
    store = _store()
    monkeypatch.setattr(platform_router, "get_platform_store", lambda: store)

    # Owner of A can read A; reaching B returns 404 (identical to non-existent).
    assert platform_router.list_apps("acct_a", principal=_admin("owner_a")) == []
    with pytest.raises(HTTPException) as cross:
        platform_router.list_apps("acct_b", principal=_admin("owner_a"))
    assert cross.value.status_code == 404

    # The global account list is scoped to accounts this admin is authorized for.
    listed = {a.id for a in platform_router.list_accounts(principal=_admin("owner_a"))}
    assert listed == {"acct_a"}


def test_granting_membership_extends_admin_access(monkeypatch):
    store = _store()
    monkeypatch.setattr(platform_router, "get_platform_store", lambda: store)

    # A stranger cannot reach acct_a...
    with pytest.raises(HTTPException):
        platform_router.list_memberships("acct_a", principal=_admin("ops@x"))

    # ...until the owner grants them an admin membership.
    platform_router.upsert_membership(
        "acct_a",
        platform_router.MembershipIn(user_id="ops@x", role_id="admin"),
        principal=_admin("owner_a"),
    )
    assert [m.user_id for m in platform_router.list_memberships("acct_a", principal=_admin("ops@x"))] == ["ops@x"]

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.config as config_mod
import app.deps as deps
from app.auth.principal import resolve_principal
from app.auth.tokens import make_session_token
from app.platform.base import (
    AccessGroup,
    AccessGroupMembership,
    Account,
    Space,
)
from app.platform.memory import MemoryPlatformStore
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore
from app.users.base import User
from app.users.memory import MemoryUserStore


ACCOUNT = "tenant_account"
SPACE = "space_shared"
USER = "user_finance"


def _platform() -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id=ACCOUNT, kind="organization", name="Acme", owner_user_id="owner"))
    platform.create_space(Space(id=SPACE, account_id=ACCOUNT, kind="business", name="Company"))
    return platform


def test_access_groups_are_stable_account_scoped_compartments():
    platform = _platform()
    group = platform.upsert_access_group(AccessGroup(
        id="department_finance",
        account_id=ACCOUNT,
        space_id=SPACE,
        name="Finance",
    ))
    membership = platform.upsert_access_group_membership(AccessGroupMembership(
        id="group_member_aaaaaaaa",
        account_id=ACCOUNT,
        space_id=SPACE,
        group_id=group.id,
        user_id=USER,
    ))

    assert platform.list_access_groups(ACCOUNT, SPACE) == [group]
    assert platform.list_access_group_memberships(ACCOUNT, USER) == [membership]
    with pytest.raises(ValueError, match="name"):
        platform.upsert_access_group(AccessGroup(
            id="department_finance_2",
            account_id=ACCOUNT,
            space_id=SPACE,
            name="finance",
        ))

    archived = platform.upsert_access_group(AccessGroup(
        id="department_archived",
        account_id=ACCOUNT,
        space_id=SPACE,
        name="Archived",
        status="archived",
    ))
    with pytest.raises(ValueError, match="Archived"):
        platform.upsert_access_group_membership(AccessGroupMembership(
            id="group_member_archived",
            account_id=ACCOUNT,
            space_id=SPACE,
            group_id=archived.id,
            user_id=USER,
        ))

    with pytest.raises(ValueError, match="immutable"):
        platform.upsert_access_group_membership(replace(membership, user_id="different_user"))
    with pytest.raises(ValueError, match="group"):
        platform.upsert_access_group_membership(AccessGroupMembership(
            id="group_member_bbbbbbbb",
            account_id=ACCOUNT,
            space_id=SPACE,
            group_id="department_missing",
            user_id=USER,
        ))


def test_identity_resolution_adds_active_department_ids_for_every_retrieval_consumer(monkeypatch):
    platform = _platform()
    platform.upsert_access_group(AccessGroup(
        id="department_finance",
        account_id=ACCOUNT,
        space_id=SPACE,
        name="Finance",
    ))
    archived_group = platform.upsert_access_group(AccessGroup(
        id="department_archived",
        account_id=ACCOUNT,
        space_id=SPACE,
        name="Archived",
    ))
    platform.upsert_access_group_membership(AccessGroupMembership(
        id="group_member_aaaaaaaa",
        account_id=ACCOUNT,
        space_id=SPACE,
        group_id="department_finance",
        user_id=USER,
    ))
    platform.upsert_access_group_membership(AccessGroupMembership(
        id="group_member_bbbbbbbb",
        account_id=ACCOUNT,
        space_id=SPACE,
        group_id="department_archived",
        user_id=USER,
    ))
    platform.upsert_access_group(replace(archived_group, status="archived"))

    users = MemoryUserStore()
    users.create(User(
        id=USER,
        email="finance@example.test",
        display_name="Finance User",
        password_hash="unused",
        tenant_id=ACCOUNT,
        role_id="front_desk",
        location="munich",
    ))
    sessions = MemorySessionStore()
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sessions.create(Session(id="session_aaaaaaaa", user_id=USER, tenant_id=ACCOUNT, expires_at=expires))
    settings = SimpleNamespace(auth_secret="unit-test-secret")
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_user_store", lambda: users)
    monkeypatch.setattr(deps, "get_session_store", lambda: sessions)
    monkeypatch.setattr(deps, "get_platform_store", lambda: platform)
    token = make_session_token(USER, "session_aaaaaaaa", settings.auth_secret, 3600)

    principal = resolve_principal(ob_session=token)

    assert "department_finance" in principal.categories
    assert "department_archived" not in principal.categories
    assert principal.access_filter().allows({
        "tenant_id": ACCOUNT,
        "classification": 1,
        "location": "munich",
        "category": "department_finance",
        "status": "approved",
    })


def test_access_group_lookup_failure_never_grants_a_department(monkeypatch):
    users = MemoryUserStore()
    users.create(User(
        id=USER,
        email="finance@example.test",
        display_name="Finance User",
        password_hash="unused",
        tenant_id=ACCOUNT,
        role_id="front_desk",
        location="munich",
    ))
    sessions = MemorySessionStore()
    sessions.create(Session(id="session_aaaaaaaa", user_id=USER, tenant_id=ACCOUNT))
    settings = SimpleNamespace(auth_secret="unit-test-secret")
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_user_store", lambda: users)
    monkeypatch.setattr(deps, "get_session_store", lambda: sessions)
    monkeypatch.setattr(
        deps,
        "get_platform_store",
        lambda: SimpleNamespace(list_access_group_memberships=lambda *_: (_ for _ in ()).throw(RuntimeError("down"))),
    )
    token = make_session_token(USER, "session_aaaaaaaa", settings.auth_secret, 3600)

    principal = resolve_principal(ob_session=token)

    assert "department_finance" not in principal.categories

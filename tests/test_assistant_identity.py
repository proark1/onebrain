"""Assistant identity handoff: the assistant resolves a OneBrain user at login.

OneBrain remains the identity authority. The endpoint verifies credentials with
the same protections as /api/auth/login (timing-safe compare, lockout) and binds
the resolved user to the calling service key's tenant.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.assistant as assistant_router
from app.assistant.contracts import ASSISTANT_PURPOSES
from app.auth.passwords import hash_password
from app.auth.principal import Principal
from app.platform.base import Account, AppInstallation, Space
from app.platform.memory import MemoryPlatformStore
from app.schemas import AssistantIdentityLoginRequest
from app.security.policy import Classification
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE
from app.users.base import User
from app.users.memory import MemoryUserStore


class FakeThrottle:
    def __init__(self, locked: int = 0) -> None:
        self.locked = locked
        self.failures: list[str] = []
        self.successes: list[str] = []

    def retry_after(self, key: str) -> int:
        return self.locked

    def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def record_success(self, key: str) -> None:
        self.successes.append(key)


def _service_principal(tenant: str = "acme") -> Principal:
    return Principal(
        user_id="svc:assistant",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,
        locations=frozenset(),
        categories=frozenset({"general"}),
        location_label="-",
        tenant_id=tenant,
        principal_type="service",
        scopes=frozenset((SCOPE_READ, SCOPE_WRITE)),
        account_id=tenant,
        app_id="assistant",
        space_ids=frozenset({"sp_business"}),
        purposes=frozenset(ASSISTANT_PURPOSES),
    )


def _setup(monkeypatch, *, locked: int = 0) -> tuple[MemoryUserStore, FakeThrottle]:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme GmbH"))
    platform.create_space(Space(id="sp_business", account_id="acme", kind="business", name="Business"))
    platform.install_app(AppInstallation(
        id="appi_assistant",
        account_id="acme",
        app_id="assistant",
        enabled_space_ids=("sp_business",),
        allowed_purposes=tuple(sorted(ASSISTANT_PURPOSES)),
    ))
    users = MemoryUserStore()
    users.create(User(
        id="user_1", email="owner@acme.test", display_name="Owner",
        password_hash=hash_password("correct-horse"), tenant_id="acme",
        role_id="admin", location="all",
    ))
    users.create(User(
        id="user_other", email="owner@other.test", display_name="Other-tenant owner",
        password_hash=hash_password("correct-horse"), tenant_id="other_tenant",
        role_id="admin", location="all",
    ))
    users.create(User(
        id="user_off", email="disabled@acme.test", display_name="Disabled",
        password_hash=hash_password("correct-horse"), tenant_id="acme",
        role_id="admin", location="all", status="disabled",
    ))
    throttle = FakeThrottle(locked=locked)
    monkeypatch.setattr(assistant_router, "_rate_limit", lambda principal: None)
    monkeypatch.setattr(assistant_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(assistant_router, "get_user_store", lambda: users)
    monkeypatch.setattr(assistant_router, "get_login_throttle", lambda: throttle)
    return users, throttle


def test_identity_login_resolves_user_account_and_space(monkeypatch):
    _setup(monkeypatch)

    response = assistant_router.assistant_identity_login(
        AssistantIdentityLoginRequest(email="Owner@acme.test", password="correct-horse"),
        principal=_service_principal(),
    )

    assert response.user_id == "user_1"
    assert response.tenant_id == "acme"
    assert response.account_id == "acme"
    assert response.space_id == "sp_business"
    assert response.display_name == "Owner"


def test_identity_login_rejects_bad_password_and_records_failure(monkeypatch):
    _, throttle = _setup(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_identity_login(
            AssistantIdentityLoginRequest(email="owner@acme.test", password="wrong"),
            principal=_service_principal(),
        )

    assert exc.value.status_code == 401
    assert throttle.failures


def test_identity_login_rejects_cross_tenant_user_indistinguishably(monkeypatch):
    _setup(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_identity_login(
            AssistantIdentityLoginRequest(email="owner@other.test", password="correct-horse"),
            principal=_service_principal(),
        )

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid email or password."


def test_identity_login_rejects_disabled_user(monkeypatch):
    _setup(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_identity_login(
            AssistantIdentityLoginRequest(email="disabled@acme.test", password="correct-horse"),
            principal=_service_principal(),
        )

    assert exc.value.status_code == 401


def test_identity_login_locks_out_after_repeated_failures(monkeypatch):
    _setup(monkeypatch, locked=30)

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_identity_login(
            AssistantIdentityLoginRequest(email="owner@acme.test", password="correct-horse"),
            principal=_service_principal(),
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "30"


def test_identity_login_requires_assistant_app_key(monkeypatch):
    _setup(monkeypatch)
    wrong_app = _service_principal()
    wrong_app = Principal(**{**wrong_app.__dict__, "app_id": "communication"})

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_identity_login(
            AssistantIdentityLoginRequest(email="owner@acme.test", password="correct-horse"),
            principal=wrong_app,
        )

    assert exc.value.status_code == 403

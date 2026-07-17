"""Revocable sessions: the layer that makes a signed cookie killable early."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

import app.auth.principal as principal_mod
import app.config as config_mod
import app.deps as deps
import app.routers.auth as auth_router
from app.auth.passwords import hash_password
from app.auth.principal import resolve_principal
from app.auth.throttle import LoginThrottle
from app.auth.tokens import make_session_token, make_token, read_session_token
from app.schemas import LoginRequest, RevokeSessionsRequest
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore
from app.users.base import User
from app.users.memory import MemoryUserStore


# --- token layer -----------------------------------------------------------------

def test_session_token_roundtrip_and_rejects_legacy_token():
    token = make_session_token("u1", "sid-123", "secret", 3600)
    assert read_session_token(token, "secret") == ("u1", "sid-123")
    assert read_session_token(token, "other") is None          # wrong key
    assert read_session_token(token + "x", "secret") is None    # tampered
    assert read_session_token(make_session_token("u", "s", "secret", -1), "secret") is None  # expired
    # A pre-revocation cookie (no sid) is refused — the holder must re-authenticate.
    assert read_session_token(make_token("u1", "secret", 3600), "secret") is None


# --- store layer -----------------------------------------------------------------

def _session(sid: str, user_id: str = "u1") -> Session:
    return Session(id=sid, user_id=user_id, tenant_id="nft_gym",
                   created_at="2026-07-11T00:00:00+00:00", expires_at="2026-07-18T00:00:00+00:00")


def test_store_create_get_and_revoke():
    store = MemorySessionStore()
    store.create(_session("s1"))
    assert store.get("s1").active is True
    assert store.revoke("s1") is True
    assert store.get("s1").active is False
    assert store.revoke("s1") is False       # already revoked
    assert store.revoke("nope") is False     # unknown


def test_store_revoke_all_for_user():
    store = MemorySessionStore()
    store.create(_session("s1", "u1"))
    store.create(_session("s2", "u1"))
    store.create(_session("s3", "u2"))
    assert store.revoke_all_for_user("u1") == 2
    assert store.get("s1").active is False and store.get("s2").active is False
    assert store.get("s3").active is True     # other user untouched
    assert store.revoke_all_for_user("u1") == 0  # idempotent


def test_store_purge_expired():
    store = MemorySessionStore()
    store.create(Session(id="old", user_id="u1", expires_at="2020-01-01T00:00:00+00:00"))
    store.create(Session(id="new", user_id="u1", expires_at="2999-01-01T00:00:00+00:00"))
    assert store.purge_expired("2026-07-11T00:00:00+00:00") == 1
    assert store.get("old") is None and store.get("new") is not None


# --- end-to-end through the router + resolve_principal ---------------------------

def _wire(monkeypatch, sessions, users):
    settings = SimpleNamespace(auth_secret="unit-test-secret", session_days=7, cookie_secure=False)
    throttle = LoginThrottle(5, 900)
    for mod in (deps,):
        monkeypatch.setattr(mod, "get_session_store", lambda: sessions)
        monkeypatch.setattr(mod, "get_user_store", lambda: users)
    monkeypatch.setattr(auth_router, "get_session_store", lambda: sessions)
    monkeypatch.setattr(auth_router, "get_user_store", lambda: users)
    monkeypatch.setattr(auth_router, "get_login_throttle", lambda: throttle)
    monkeypatch.setattr(auth_router, "get_settings", lambda: settings)
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(principal_mod, "get_settings", lambda: settings, raising=False)
    return settings


def _user(user_id="u1", email="alice@nftgym.de", role_id="admin") -> User:
    return User(id=user_id, email=email, display_name="Alice",
                password_hash=hash_password("pw"), tenant_id="nft_gym", role_id=role_id, location="")


def _cookie_token(resp: Response) -> str:
    raw = resp.headers.get("set-cookie", "")
    head = raw.split(";", 1)[0]
    assert head.startswith("ob_session=")
    return head[len("ob_session="):]


def test_login_issues_working_session_then_logout_revokes(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_user())
    _wire(monkeypatch, sessions, users)

    resp = Response()
    auth_router.login(LoginRequest(email="alice@nftgym.de", password="pw"), resp)
    token = _cookie_token(resp)

    # The freshly-minted cookie authenticates.
    principal = resolve_principal(ob_session=token)
    assert principal.user_id == "u1" and principal.role_id == "admin"

    # After logout the same token is dead on the next request.
    auth_router.logout(Response(), ob_session=token)
    with pytest.raises(HTTPException) as exc:
        resolve_principal(ob_session=token)
    assert exc.value.status_code == 401


def test_offboarding_revoke_forces_reauth(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_user("admin1", "boss@nftgym.de", "admin"))
    users.create(_user("emp1", "emp@nftgym.de", "front_desk"))
    _wire(monkeypatch, sessions, users)

    resp = Response()
    auth_router.login(LoginRequest(email="emp@nftgym.de", password="pw"), resp)
    emp_token = _cookie_token(resp)
    assert resolve_principal(ob_session=emp_token).user_id == "emp1"

    admin = resolve_principal(ob_session=_login_token(monkeypatch, sessions, users, "boss@nftgym.de"))
    out = auth_router.revoke_sessions(RevokeSessionsRequest(email="emp@nftgym.de"), principal=admin)
    assert out["sessions_revoked"] == 1

    with pytest.raises(HTTPException) as exc:
        resolve_principal(ob_session=emp_token)
    assert exc.value.status_code == 401


def test_revoke_sessions_requires_admin_and_same_tenant(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_user("admin1", "boss@nftgym.de", "admin"))
    users.create(_user("emp1", "emp@nftgym.de", "front_desk"))
    _wire(monkeypatch, sessions, users)

    non_admin = principal_mod.principal_from_user(users.get("emp1"))
    with pytest.raises(HTTPException) as exc:
        auth_router.revoke_sessions(RevokeSessionsRequest(email="boss@nftgym.de"), principal=non_admin)
    assert exc.value.status_code == 403

    admin = principal_mod.principal_from_user(users.get("admin1"))
    with pytest.raises(HTTPException) as exc:
        auth_router.revoke_sessions(RevokeSessionsRequest(email="nobody@other.de"), principal=admin)
    assert exc.value.status_code == 404


def test_resolve_principal_rejects_revoked_missing_and_disabled(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_user())
    _wire(monkeypatch, sessions, users)

    resp = Response()
    auth_router.login(LoginRequest(email="alice@nftgym.de", password="pw"), resp)
    token = _cookie_token(resp)
    _, sid = read_session_token(token, "unit-test-secret")

    # A token whose session row was dropped entirely is rejected (no silent trust).
    other = make_session_token("u1", "missing-sid", "unit-test-secret", 3600)
    with pytest.raises(HTTPException):
        resolve_principal(ob_session=other)

    # A disabled user is rejected even with a live session.
    users.get("u1").status = "disabled"
    with pytest.raises(HTTPException):
        resolve_principal(ob_session=token)
    users.get("u1").status = "active"
    assert resolve_principal(ob_session=token).user_id == "u1"

    sessions.revoke(sid)
    with pytest.raises(HTTPException):
        resolve_principal(ob_session=token)


def test_resolve_principal_rejects_expired_session_even_with_live_token(monkeypatch):
    # A session row that has expired must fail auth even if the signed token still
    # has TTL left — the row is the source of truth for expiry as well as revocation.
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_user())
    _wire(monkeypatch, sessions, users)

    sessions.create(Session(id="sid-exp", user_id="u1", tenant_id="nft_gym",
                            expires_at="2000-01-01T00:00:00+00:00"))
    token = make_session_token("u1", "sid-exp", "unit-test-secret", 3600)  # token not expired

    with pytest.raises(HTTPException) as exc:
        resolve_principal(ob_session=token)
    assert exc.value.status_code == 401


def test_me_surfaces_operator_and_password_change_flags(monkeypatch):
    # /api/session/me carries the Mission Control signals so the console can render
    # admin-only (operator_mode) and gate the Control/Fleet tabs (is_operator_surface).
    import app.routers.session as session_router

    users = MemoryUserStore()
    users.create(_user())
    principal = principal_mod.principal_from_user(users.get("u1"))

    monkeypatch.setattr(session_router, "get_settings",
                        lambda: SimpleNamespace(operator_mode=True, is_operator_surface=True))
    mc = session_router.me(principal=principal)
    assert mc.operator_mode is True and mc.is_operator_surface is True

    monkeypatch.setattr(session_router, "get_settings",
                        lambda: SimpleNamespace(operator_mode=False, is_operator_surface=False))
    customer = session_router.me(principal=principal)
    assert customer.operator_mode is False and customer.is_operator_surface is False
    # Identity fields still flow through unchanged.
    assert customer.email == "alice@nftgym.de" and customer.role_id == "admin"

    required_principal = principal_mod.principal_from_user(
        User(id="u2", email="rotate@nftgym.de", display_name="Rotate",
             password_hash=hash_password("pw"), tenant_id="nft_gym", role_id="admin",
             location="", must_change_password=True)
    )
    assert session_router.me(principal=required_principal).must_change_password is True


def test_session_is_expired_helper():
    live = Session(id="s", user_id="u", expires_at="2999-01-01T00:00:00+00:00")
    dead = Session(id="s", user_id="u", expires_at="2000-01-01T00:00:00+00:00")
    forever = Session(id="s", user_id="u", expires_at="")
    now = "2026-07-11T00:00:00+00:00"
    assert live.is_expired(now) is False
    assert dead.is_expired(now) is True
    assert forever.is_expired(now) is False


def _login_token(monkeypatch, sessions, users, email) -> str:
    resp = Response()
    auth_router.login(LoginRequest(email=email, password="pw"), resp)
    return _cookie_token(resp)

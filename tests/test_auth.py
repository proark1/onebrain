"""Password hashing, session tokens, and login/principal mapping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

import app.config as config_mod
import app.deps as deps
import app.auth.principal as principal_mod
import app.routers.auth as auth_router
from app.auth.passwords import hash_password, verify_password
from app.auth.principal import principal_from_user, resolve_principal
from app.auth.throttle import LoginThrottle
from app.auth.tokens import make_token, read_token
from app.config import Settings
from app.schemas import LoginRequest
from app.security.policy import Classification
from app.sessions.memory import MemorySessionStore
from app.users.base import User
from app.users.memory import MemoryUserStore
from app.users.seed import DEMO_PASSWORD, seed_admin_from_env, seed_users_if_empty


def test_password_hash_roundtrip_and_salt():
    h = hash_password("s3cret!")
    assert verify_password("s3cret!", h)
    assert not verify_password("wrong", h)
    assert h != hash_password("s3cret!")            # random salt -> different each time
    assert not verify_password("s3cret!", "garbage")


def test_token_roundtrip_tamper_and_expiry():
    token = make_token("user-1", "secret", 3600)
    assert read_token(token, "secret") == "user-1"
    assert read_token(token, "other-secret") is None        # wrong signing key
    assert read_token(token + "x", "secret") is None         # tampered signature
    assert read_token("not-a-token", "secret") is None
    assert read_token(make_token("u", "secret", -1), "secret") is None  # expired


def test_seed_and_credential_check():
    store = MemoryUserStore()
    assert seed_users_if_empty(store) == 8
    assert seed_users_if_empty(store) == 0                   # idempotent
    hr = store.get_by_email("HR@nftgym.de")                  # case-insensitive
    assert hr and hr.role_id == "hr"
    assert verify_password(DEMO_PASSWORD, hr.password_hash)
    assert not verify_password("nope", hr.password_hash)


def test_seed_admin_from_env_creates_real_admin_and_is_idempotent():
    store = MemoryUserStore()
    settings = SimpleNamespace(admin_email="Boss@NFTGym.de", admin_password="a-strong-one")
    assert seed_admin_from_env(store, settings) == 1
    assert seed_admin_from_env(store, settings) == 0          # idempotent
    admin = store.get_by_email("boss@nftgym.de")             # normalised
    assert admin and admin.role_id == "admin" and admin.tenant_id == "nft_gym"
    assert verify_password("a-strong-one", admin.password_hash)


def test_seed_admin_from_env_is_noop_without_config():
    store = MemoryUserStore()
    assert seed_admin_from_env(store, SimpleNamespace(admin_email="", admin_password="")) == 0
    assert store.count() == 0


def test_delete_by_email_rotates_off_demo_accounts():
    store = MemoryUserStore()
    seed_users_if_empty(store)
    assert store.get_by_email("admin@nftgym.de")
    assert store.delete_by_email("Admin@nftgym.de") is True   # case-insensitive
    assert store.get_by_email("admin@nftgym.de") is None
    assert store.delete_by_email("admin@nftgym.de") is False  # already gone


def test_is_local_stack_gates_demo_seeding():
    # main.py seeds shared-password demos only when this is True (or explicit opt-in).
    assert Settings(llm_provider="local", embeddings_provider="local", vector_store="memory").is_local_stack is True
    assert Settings(llm_provider="litellm", embeddings_provider="litellm", vector_store="pgvector").is_local_stack is False


def test_principal_from_user_maps_role_and_scope():
    store = MemoryUserStore()
    seed_users_if_empty(store)

    hr = principal_from_user(store.get_by_email("hr@nftgym.de"))
    assert hr.role_id == "hr" and hr.tenant_id == "nft_gym"
    assert hr.clearance == Classification.RESTRICTED
    assert hr.locations is None                              # chain-wide
    assert hr.user_id == store.get_by_email("hr@nftgym.de").id

    fd = principal_from_user(store.get_by_email("frontdesk.munich@nftgym.de"))
    assert fd.role_id == "front_desk"
    assert fd.locations == frozenset({"munich"})


# --- P4-04: first-login one-time password + must_change_password (H-10) --------

def _mc_wire(monkeypatch, sessions, users):
    """Wire the auth router + resolve_principal against in-memory stores (mirrors
    tests/test_sessions.py::_wire)."""
    settings = SimpleNamespace(auth_secret="unit-test-secret", session_days=7, cookie_secure=False)
    monkeypatch.setattr(deps, "get_session_store", lambda: sessions)
    monkeypatch.setattr(deps, "get_user_store", lambda: users)
    monkeypatch.setattr(auth_router, "get_session_store", lambda: sessions)
    monkeypatch.setattr(auth_router, "get_user_store", lambda: users)
    monkeypatch.setattr(auth_router, "get_login_throttle", lambda: LoginThrottle(5, 900))
    monkeypatch.setattr(auth_router, "get_settings", lambda: settings)
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(principal_mod, "get_settings", lambda: settings, raising=False)


def _mc_user(must_change=True, pw="OldPassw0rd!!") -> User:
    return User(id="u1", email="owner@x.de", display_name="Owner",
                password_hash=hash_password(pw), tenant_id="acct", role_id="admin",
                location="", must_change_password=must_change)


def _login_token(sessions, users) -> str:
    resp = Response()
    auth_router.login(LoginRequest(email="owner@x.de", password="OldPassw0rd!!"), resp)
    head = resp.headers.get("set-cookie", "").split(";", 1)[0]
    assert head.startswith("ob_session=")
    return head[len("ob_session="):]


def _req(endpoint):
    return SimpleNamespace(scope={"endpoint": endpoint})


def test_login_surfaces_must_change_password(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_mc_user(must_change=True))
    _mc_wire(monkeypatch, sessions, users)

    resp = Response()
    out = auth_router.login(LoginRequest(email="owner@x.de", password="OldPassw0rd!!"), resp)
    assert out["must_change_password"] is True
    assert resp.headers.get("set-cookie", "").startswith("ob_session=")   # session still issued


def test_ip_lockout_does_not_consume_account_budget(monkeypatch):
    class RecordingThrottle:
        def __init__(self):
            self.reserved: list[str] = []
            self.released: list[str] = []

        def reserve(self, key: str) -> int:
            self.reserved.append(key)
            return 9 if key.startswith("ip:") else 0

        def release_success(self, key: str) -> None:
            self.released.append(key)

    throttle = RecordingThrottle()
    settings = SimpleNamespace(
        auth_secret="unit-test-secret",
        session_days=7,
        cookie_secure=False,
        trusted_proxy_cidrs="",
        trusted_proxy_hops=0,
    )
    monkeypatch.setattr(auth_router, "get_login_throttle", lambda: throttle)
    monkeypatch.setattr(auth_router, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc:
        auth_router.login(LoginRequest(email="victim@example.test", password="irrelevant"), Response())

    assert exc.value.status_code == 429
    assert throttle.reserved == ["account:victim@example.test", "ip:unknown"]
    assert throttle.released == ["account:victim@example.test"]


def test_resolve_principal_blocks_until_password_changed(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_mc_user(must_change=True))
    _mc_wire(monkeypatch, sessions, users)
    token = _login_token(sessions, users)

    from app.routers.session import me as session_me

    # An allowlisted (module, name) endpoint returns the principal.
    principal = resolve_principal(ob_session=token, request=_req(session_me))
    assert principal.must_change_password is True

    # A non-allowlisted endpoint -> 403 password_change_required.
    def dashboard():
        return None
    with pytest.raises(HTTPException) as exc:
        resolve_principal(ob_session=token, request=_req(dashboard))
    assert exc.value.status_code == 403 and exc.value.detail == "password_change_required"

    # A11 regression: a callable NAMED `me` but in a DIFFERENT module (this test
    # module, not app.routers.session) is STILL blocked — a bare-name match would
    # have wrongly allowed it.
    def me():
        return None
    with pytest.raises(HTTPException) as exc2:
        resolve_principal(ob_session=token, request=_req(me))
    assert exc2.value.status_code == 403

    # No request at all (direct/programmatic call) is treated as non-allowlisted.
    with pytest.raises(HTTPException):
        resolve_principal(ob_session=token)


def test_change_password_clears_flag_and_revokes_sessions(monkeypatch):
    sessions, users = MemorySessionStore(), MemoryUserStore()
    users.create(_mc_user(must_change=True))
    _mc_wire(monkeypatch, sessions, users)
    _login_token(sessions, users)   # a live session to burn
    principal = principal_from_user(users.get("u1"))

    # Wrong current password -> 401.
    with pytest.raises(HTTPException) as e1:
        auth_router.change_password(
            auth_router.ChangePasswordRequest(current_password="nope!!", new_password="BrandNewPass123"),
            principal=principal)
    assert e1.value.status_code == 401

    # New == current -> 400.
    with pytest.raises(HTTPException) as e2:
        auth_router.change_password(
            auth_router.ChangePasswordRequest(current_password="OldPassw0rd!!", new_password="OldPassw0rd!!"),
            principal=principal)
    assert e2.value.status_code == 400

    # Valid change -> flag cleared in the store + all sessions revoked.
    out = auth_router.change_password(
        auth_router.ChangePasswordRequest(current_password="OldPassw0rd!!", new_password="BrandNewPass123"),
        principal=principal)
    assert out["ok"] is True and out["sessions_revoked"] >= 1
    updated = users.get("u1")
    assert updated.must_change_password is False
    assert verify_password("BrandNewPass123", updated.password_hash)

    # A subsequent resolve for the same user no longer 403s (the flag is cleared).
    # Re-login with the NEW password (the change revoked the old session).
    def dashboard():
        return None
    resp = Response()
    auth_router.login(LoginRequest(email="owner@x.de", password="BrandNewPass123"), resp)
    new_token = resp.headers.get("set-cookie", "").split(";", 1)[0][len("ob_session="):]
    assert resolve_principal(ob_session=new_token, request=_req(dashboard)).user_id == "u1"


def test_change_password_rejects_short_new_password():
    import pydantic
    # <12 chars fails the pydantic model (FastAPI surfaces this as 422).
    with pytest.raises(pydantic.ValidationError):
        auth_router.ChangePasswordRequest(current_password="whatever", new_password="short")


def test_update_password_store_roundtrip():
    users = MemoryUserStore()
    users.create(_mc_user(must_change=True))
    updated = users.update_password("u1", hash_password("newpassword123"), must_change_password=False)
    assert updated.must_change_password is False
    assert verify_password("newpassword123", updated.password_hash)
    assert users.get("u1").must_change_password is False
    with pytest.raises(KeyError):
        users.update_password("nope", "h", must_change_password=False)


def test_postgres_user_row_maps_must_change_password_at_index_9():
    # C4 positional mapper: a swapped index would first manifest on production
    # Railway (no live Postgres harness here), so feed a synthetic 10-slot tuple.
    from app.users.postgres import PostgresUserStore

    store = object.__new__(PostgresUserStore)
    user = store._row(("u1", "a@x.de", "A", "hash", "tenant", "admin", "loc", "active", None, True))
    assert user.must_change_password is True
    assert user.id == "u1" and user.status == "active" and user.created_at == ""
    assert store._row(("u2", "b@x.de", "B", "h", "t", "admin", "", "active", None, False)).must_change_password is False

"""Password hashing, session tokens, and login/principal mapping."""

from __future__ import annotations

from app.auth.passwords import hash_password, verify_password
from app.auth.principal import principal_from_user
from app.auth.tokens import make_token, read_token
from app.security.policy import Classification
from app.users.memory import MemoryUserStore
from app.users.seed import DEMO_PASSWORD, seed_users_if_empty


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

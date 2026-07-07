"""Password hashing, session tokens, and login/principal mapping."""

from __future__ import annotations

from types import SimpleNamespace

from app.auth.passwords import hash_password, verify_password
from app.auth.principal import principal_from_user
from app.auth.tokens import make_token, read_token
from app.config import Settings
from app.security.policy import Classification
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

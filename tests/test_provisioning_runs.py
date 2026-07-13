"""P5-00 shared foundations: the re-readable seal_bundle/open_bundle cipher pair
(G1-4 / G2-1) and the memory/postgres bundle + bootstrap-token store methods."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.provisioning.runs import (
    BoxBootstrapToken,
    BoxSecretBundle,
    MemoryProvisioningRunStore,
    OneTimeSecretCipher,
    OneTimeSecretEnvelope,
    ProvisioningRun,
)


def _cipher_settings(ttl_seconds: int = 3600):
    return SimpleNamespace(
        secret_encryption_key=Fernet.generate_key().decode("utf-8"),
        secret_encryption_key_version="test",
        bootstrap_secret_ttl_seconds=ttl_seconds,
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --- re-readable bundle cipher (G1-4 / G2-1) ----------------------------------

def test_seal_open_bundle_round_trips():
    cipher = OneTimeSecretCipher(_cipher_settings())
    ciphertext = cipher.seal_bundle("POSTGRES_PASSWORD=abc\nREDIS_PASSWORD=def\n")
    assert cipher.open_bundle(ciphertext) == "POSTGRES_PASSWORD=abc\nREDIS_PASSWORD=def\n"


def test_bundle_ciphertext_decrypts_twice_not_single_read():
    # The one-time envelope path is single-read (decrypt raises once read_at is set);
    # the bundle pair MUST be re-readable — the SAME ciphertext opens repeatedly.
    cipher = OneTimeSecretCipher(_cipher_settings())
    ciphertext = cipher.seal_bundle("v")
    assert cipher.open_bundle(ciphertext) == "v"
    assert cipher.open_bundle(ciphertext) == "v"
    assert cipher.open_bundle(ciphertext) == "v"


def test_bundle_open_is_not_ttl_gated_unlike_envelope():
    # A TTL that has already elapsed rejects the ONE-TIME envelope path...
    cipher = OneTimeSecretCipher(_cipher_settings(ttl_seconds=1))
    ciphertext = cipher.seal_bundle("v")
    past = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    expired_envelope = OneTimeSecretEnvelope(
        id="ots_x", purpose="p", account_id="", deployment_id="",
        ciphertext=ciphertext, nonce="", key_version="test", expires_at=past,
    )
    with pytest.raises(ValueError):
        cipher.decrypt(expired_envelope)
    # ...but the re-readable bundle pair opens it regardless of any TTL.
    assert cipher.open_bundle(ciphertext) == "v"


def test_open_bundle_raises_value_error_on_invalid_token():
    cipher = OneTimeSecretCipher(_cipher_settings())
    with pytest.raises(ValueError):
        cipher.open_bundle("not-a-valid-fernet-token")


# --- memory store: secret bundles ---------------------------------------------

def test_upsert_and_get_secret_bundle():
    store = MemoryProvisioningRunStore()
    stored = store.upsert_secret_bundle(BoxSecretBundle(
        deployment_id="dep1", account_id="acct1", ciphertext="ct", secrets_epoch=0))
    assert stored.updated_at  # stamped
    got = store.get_secret_bundle("dep1")
    assert got is not None
    assert got.deployment_id == "dep1"
    assert got.account_id == "acct1"
    assert got.ciphertext == "ct"
    assert got.secrets_epoch == 0
    assert store.get_secret_bundle("missing") is None


def test_bump_secrets_epoch_increments_and_reseal_preserves_epoch():
    store = MemoryProvisioningRunStore()
    store.upsert_secret_bundle(BoxSecretBundle(deployment_id="dep1", account_id="a", ciphertext="ct0"))
    assert store.bump_secrets_epoch("dep1") == 1
    assert store.bump_secrets_epoch("dep1") == 2
    # A re-seal (new ciphertext) must NOT reset the running epoch — bump owns it.
    resealed = store.upsert_secret_bundle(BoxSecretBundle(
        deployment_id="dep1", account_id="a", ciphertext="ct1", secrets_epoch=0))
    assert resealed.secrets_epoch == 2
    assert resealed.ciphertext == "ct1"


def test_bump_secrets_epoch_unknown_raises():
    store = MemoryProvisioningRunStore()
    with pytest.raises(ValueError):
        store.bump_secrets_epoch("nope")


# --- memory store: bootstrap tokens (single-use + expiry, G1-8) ---------------

def _token(token_hash="sha256$deadbeef", *, ttl_seconds=3600, deployment_id="dep1"):
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return BoxBootstrapToken(
        token_hash=token_hash, deployment_id=deployment_id, account_id="acct1",
        expires_at=_iso(expires))


def test_create_and_get_bootstrap_token():
    store = MemoryProvisioningRunStore()
    created = store.create_bootstrap_token(_token())
    assert created.created_at  # stamped
    got = store.get_bootstrap_token("sha256$deadbeef")
    assert got is not None and got.deployment_id == "dep1" and got.consumed_at == ""


def test_consume_bootstrap_token_is_single_use():
    store = MemoryProvisioningRunStore()
    store.create_bootstrap_token(_token())
    consumed = store.consume_bootstrap_token("sha256$deadbeef")
    assert consumed is not None and consumed.consumed_at
    # A second consume (replay) returns None — atomically single-use.
    assert store.consume_bootstrap_token("sha256$deadbeef") is None
    # get still shows it, now consumed (validation path can distinguish).
    assert store.get_bootstrap_token("sha256$deadbeef").consumed_at


def test_consume_expired_bootstrap_token_returns_none():
    store = MemoryProvisioningRunStore()
    store.create_bootstrap_token(_token(ttl_seconds=-10))  # already expired
    # Expiry enforced in the SAME guard as single-use (G1-8): an unconsumed but
    # expired token cannot be burned.
    assert store.consume_bootstrap_token("sha256$deadbeef") is None
    still = store.get_bootstrap_token("sha256$deadbeef")
    assert still is not None and still.consumed_at == ""  # never consumed


def test_consume_unknown_bootstrap_token_returns_none():
    store = MemoryProvisioningRunStore()
    assert store.consume_bootstrap_token("sha256$nope") is None


def test_create_duplicate_bootstrap_token_raises():
    store = MemoryProvisioningRunStore()
    store.create_bootstrap_token(_token())
    with pytest.raises(ValueError):
        store.create_bootstrap_token(_token())


# --- memory persistence round-trip (additive, back-compatible) ----------------

def test_memory_persist_round_trips_bundles_and_tokens(tmp_path):
    path = str(tmp_path / "provisioning.json")
    store = MemoryProvisioningRunStore(persist_path=path)
    store.upsert_secret_bundle(BoxSecretBundle(deployment_id="dep1", account_id="a", ciphertext="ct"))
    store.bump_secrets_epoch("dep1")
    store.create_bootstrap_token(_token())

    reloaded = MemoryProvisioningRunStore(persist_path=path)
    bundle = reloaded.get_secret_bundle("dep1")
    assert bundle is not None and bundle.secrets_epoch == 1
    assert reloaded.get_bootstrap_token("sha256$deadbeef") is not None


# --- postgres positional mappers + arity (C4) ---------------------------------

def _bare_postgres_provisioning_store():
    from app.provisioning.runs import PostgresProvisioningRunStore

    # Skip __init__ (DSN + schema validation) — the mappers are pure.
    return object.__new__(PostgresProvisioningRunStore)


def test_postgres_bundle_and_token_mappers_positional():
    store = _bare_postgres_provisioning_store()
    updated = datetime(2026, 7, 12, 1, 2, 3, tzinfo=timezone.utc)
    bundle = store._bundle(("dep0", "acct1", "ct2", "kv3", 4, updated))
    assert bundle.deployment_id == "dep0"
    assert bundle.account_id == "acct1"
    assert bundle.ciphertext == "ct2"
    assert bundle.key_version == "kv3"
    assert bundle.secrets_epoch == 4
    assert bundle.updated_at == updated.isoformat()

    expires = datetime(2026, 7, 12, 4, 5, 6, tzinfo=timezone.utc)
    consumed = datetime(2026, 7, 12, 7, 8, 9, tzinfo=timezone.utc)
    created = datetime(2026, 7, 12, 10, 11, 12, tzinfo=timezone.utc)
    token = store._token(("sha256$h0", "dep1", "acct2", expires, consumed, created))
    assert token.token_hash == "sha256$h0"
    assert token.deployment_id == "dep1"
    assert token.account_id == "acct2"
    assert token.expires_at == expires.isoformat()
    assert token.consumed_at == consumed.isoformat()
    assert token.created_at == created.isoformat()


def test_postgres_bundle_token_cols_arity_matches_mapper():
    from app.provisioning.runs import PostgresProvisioningRunStore

    assert len(PostgresProvisioningRunStore._BUNDLE_COLS.split(",")) == 6  # _bundle reads 0..5
    assert len(PostgresProvisioningRunStore._TOKEN_COLS.split(",")) == 6   # _token reads 0..5


def test_postgres_run_update_persists_external_provider():
    """A provider dispatcher may reclassify a newly-created run (for example,
    github_actions -> hetzner); the Postgres update must persist that field."""
    store = _bare_postgres_provisioning_store()
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            provider = (
                captured["params"][1]
                if "external_provider = %s" in captured["sql"]
                else "github_actions"
            )
            return (
                "prun_1", "acct_1", "dep_1", "bundle_1", "admin", "dispatched",
                provider, "", "dev.example", {}, {}, "hetzner:123", "onebrain-dep-1",
                {}, "", "", "", "", "", None, None, None, None,
            )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    store._conn = lambda: Connection()
    run = ProvisioningRun(
        id="prun_1",
        account_id="acct_1",
        deployment_id="dep_1",
        bundle_id="bundle_1",
        requested_by="admin",
        status="dispatched",
        external_provider="hetzner",
        external_run_url="dev.example",
        railway_project_id="hetzner:123",
        railway_environment_id="onebrain-dep-1",
    )

    updated = store.update_run(run)

    assert updated.external_provider == "hetzner"
    assert "external_provider = %s" in captured["sql"]

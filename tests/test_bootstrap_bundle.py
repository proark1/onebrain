"""The pure secret-bundle contract (P5-00 · §1.5)."""

from __future__ import annotations

from app.fleet.bootstrap_bundle import (
    BUNDLE_KEYS,
    OPTIONAL_KEYS,
    REQUIRED_KEYS,
    RUNTIME_BUNDLE_BACKFILL_KEYS,
    RUNTIME_DB_PASSWORD_KEYS,
    URLENCODED_SECRET_ALIASES,
    backfill_runtime_db_passwords,
    render_dotenv,
    validate_bundle,
)


def _full_bundle(**overrides) -> dict:
    bundle = {
        "POSTGRES_PASSWORD": "pg-secret",
        "POSTGRES_APP_PASSWORD": "a" * 64,
        "POSTGRES_WORKER_PASSWORD": "w" * 64,
        "POSTGRES_ASSISTANT_PASSWORD": "s" * 64,
        "POSTGRES_COMMUNICATION_PASSWORD": "c" * 64,
        "REDIS_PASSWORD": "redis-secret",
        "ONEBRAIN_FLEET_KEY": "fk_abc_def",
        "ONEBRAIN_LLM_API_KEY": "",
        "ONEBRAIN_AUTH_SECRET": "a" * 64,   # >= MIN_KEY_LENGTHS floor (32); token_hex(32) in prod
        "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET": "b" * 64,
        "ONEBRAIN_ADMIN_EMAIL": "owner@example.com",
        "ONEBRAIN_ADMIN_PASSWORD": "owner-otp",
        "ONEBRAIN_SERVICE_KEY": "",
        "ONEBRAIN_SPACE_ID": "",
        "ONEBRAIN_ASSISTANT_SERVICE_KEY": "",
        "ONEBRAIN_COMMUNICATION_SERVICE_KEY": "",
        "ONEBRAIN_COMMUNICATION_SPACE_ID": "",
        "ONEBRAIN_CUSTOMER_BOOTSTRAP": "eyJhY2NvdW50X2lkIjoiYWNjdF8xIn0",
        "UPDATE_BACKUP_KEY": "backup-secret",
        "UPDATE_DESIRED_STATE_PUBLIC_KEYS": "pub1,pub2",
        "ONEBRAIN_DNS_TOKEN": "",
        "ONEBRAIN_BACKUP_S3_ACCESS_KEY": "",
        "ONEBRAIN_BACKUP_S3_SECRET_KEY": "",
    }
    bundle.update(overrides)
    return bundle


def test_callback_token_is_never_a_bundle_key():
    # G1-7: the callback token stays baked in box.env, NEVER in the exchange bundle.
    assert "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN" not in BUNDLE_KEYS
    assert "ONEBRAIN_BOOTSTRAP_TOKEN" not in BUNDLE_KEYS
    assert all(alias not in BUNDLE_KEYS for _, alias in URLENCODED_SECRET_ALIASES)


def test_required_and_optional_partition_bundle_keys():
    assert set(REQUIRED_KEYS) | set(OPTIONAL_KEYS) == set(BUNDLE_KEYS)
    assert set(REQUIRED_KEYS) & set(OPTIONAL_KEYS) == set()
    # The two explicitly-optional keys named in the spec.
    assert "ONEBRAIN_DNS_TOKEN" in OPTIONAL_KEYS
    assert "ONEBRAIN_SPACE_ID" in OPTIONAL_KEYS
    assert "ONEBRAIN_ASSISTANT_SERVICE_KEY" in OPTIONAL_KEYS
    assert "ONEBRAIN_COMMUNICATION_SERVICE_KEY" in OPTIONAL_KEYS
    assert "ONEBRAIN_COMMUNICATION_SPACE_ID" in OPTIONAL_KEYS


def test_render_dotenv_emits_only_present_keys_in_canonical_order_lf():
    dotenv = render_dotenv(_full_bundle())
    # LF only, no CR.
    assert "\r" not in dotenv
    lines = dotenv.splitlines()
    # Every bundle key is followed by its derived URL-safe connection-secret
    # aliases.  The aliases are not persisted bundle keys.
    assert [line.split("=", 1)[0] for line in lines] == [
        *BUNDLE_KEYS,
        *(alias for _, alias in URLENCODED_SECRET_ALIASES),
    ]
    assert "POSTGRES_PASSWORD=pg-secret" in lines
    assert "UPDATE_DESIRED_STATE_PUBLIC_KEYS=pub1,pub2" in lines
    # Present-but-empty keys are emitted as bare KEY= (no quoting).
    assert "ONEBRAIN_DNS_TOKEN=" in lines
    # Trailing newline (standard dotenv).
    assert dotenv.endswith("\n")


def test_render_dotenv_skips_absent_keys_and_ignores_extras():
    bundle = {"POSTGRES_PASSWORD": "x", "REDIS_PASSWORD": "y", "NOT_A_BUNDLE_KEY": "z"}
    dotenv = render_dotenv(bundle)
    assert dotenv == (
        "POSTGRES_PASSWORD=x\n"
        "REDIS_PASSWORD=y\n"
        "POSTGRES_PASSWORD_URLENCODED=x\n"
        "REDIS_PASSWORD_URLENCODED=y\n"
    )
    assert "NOT_A_BUNDLE_KEY" not in dotenv


def test_render_dotenv_does_not_quote_secret_values():
    dotenv = render_dotenv(_full_bundle(POSTGRES_PASSWORD="a b/c=+d"))
    assert "POSTGRES_PASSWORD=a b/c=+d\n" in dotenv


def test_render_dotenv_derives_urlencoded_connection_password_aliases():
    raw = "p@ss:/?[]+=%"
    dotenv = render_dotenv(_full_bundle(
        POSTGRES_PASSWORD=raw,
        POSTGRES_APP_PASSWORD=raw,
        POSTGRES_WORKER_PASSWORD=raw,
        POSTGRES_ASSISTANT_PASSWORD=raw,
        POSTGRES_COMMUNICATION_PASSWORD=raw,
        REDIS_PASSWORD=raw,
    ))
    encoded = "p%40ss%3A%2F%3F%5B%5D%2B%3D%25"

    # Init services receive raw values, while URL consumers receive aliases
    # derived from precisely the same values.
    for source, alias in URLENCODED_SECRET_ALIASES:
        assert f"{source}={raw}\n" in dotenv
        assert f"{alias}={encoded}\n" in dotenv


def test_validate_bundle_accepts_full_bundle_with_empty_optionals():
    # DNS token, space id, service/LLM keys, pubkey set all empty -> still valid.
    assert validate_bundle(_full_bundle()) == []


def test_validate_bundle_flags_missing_required_key():
    bundle = _full_bundle()
    del bundle["POSTGRES_PASSWORD"]
    errors = validate_bundle(bundle)
    assert any("POSTGRES_PASSWORD" in e for e in errors)


def test_validate_bundle_flags_empty_required_key():
    errors = validate_bundle(_full_bundle(ONEBRAIN_FLEET_KEY="   "))
    assert any("ONEBRAIN_FLEET_KEY" in e for e in errors)


def test_admin_email_is_a_required_bundle_key():
    # A box with no admin email seeds no admin (seed.py needs BOTH email + password) and,
    # with SSH closed, is permanently unreachable — so the email is REQUIRED, fail closed.
    assert "ONEBRAIN_ADMIN_EMAIL" in REQUIRED_KEYS
    assert "ONEBRAIN_ADMIN_EMAIL" in BUNDLE_KEYS
    # Missing -> flagged.
    missing = _full_bundle()
    del missing["ONEBRAIN_ADMIN_EMAIL"]
    assert any("ONEBRAIN_ADMIN_EMAIL" in e for e in validate_bundle(missing))
    # Present-but-empty (incl. whitespace-only) -> flagged.
    assert any("ONEBRAIN_ADMIN_EMAIL" in e for e in validate_bundle(_full_bundle(ONEBRAIN_ADMIN_EMAIL="")))
    assert any("ONEBRAIN_ADMIN_EMAIL" in e for e in validate_bundle(_full_bundle(ONEBRAIN_ADMIN_EMAIL="  ")))


def test_validate_bundle_allows_empty_optional_desired_state_set():
    # Inert default: no wrapper key configured -> empty pubkey set is valid.
    assert validate_bundle(_full_bundle(UPDATE_DESIRED_STATE_PUBLIC_KEYS="")) == []


def test_auth_secret_is_a_required_bundle_key():
    # A box with no ONEBRAIN_AUTH_SECRET crashes onebrain-api on startup (app/main.py refuses
    # to boot without a strong cookie secret), so it is REQUIRED — never provision it.
    from app.fleet.bootstrap_bundle import MIN_KEY_LENGTHS

    assert "ONEBRAIN_AUTH_SECRET" in REQUIRED_KEYS
    assert "ONEBRAIN_AUTH_SECRET" in BUNDLE_KEYS
    assert MIN_KEY_LENGTHS["ONEBRAIN_AUTH_SECRET"] == 32
    missing = _full_bundle()
    del missing["ONEBRAIN_AUTH_SECRET"]
    assert any("ONEBRAIN_AUTH_SECRET" in e for e in validate_bundle(missing))
    assert any("ONEBRAIN_AUTH_SECRET" in e for e in validate_bundle(_full_bundle(ONEBRAIN_AUTH_SECRET="")))


def test_login_rate_limit_secret_is_a_distinct_required_bundle_key():
    from app.fleet.bootstrap_bundle import MIN_KEY_LENGTHS

    assert "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET" in REQUIRED_KEYS
    assert "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET" in BUNDLE_KEYS
    assert MIN_KEY_LENGTHS["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"] == 32
    missing = _full_bundle()
    del missing["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"]
    assert any("ONEBRAIN_LOGIN_RATE_LIMIT_SECRET" in e for e in validate_bundle(missing))
    assert any(
        "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET" in error
        for error in validate_bundle(_full_bundle(ONEBRAIN_LOGIN_RATE_LIMIT_SECRET="short"))
    )


def test_validate_bundle_flags_weak_auth_secret_below_floor():
    # Present-but-too-short is as fatal as missing: app/main.py's >=32 guard would boot-loop
    # the box, so a <32-char ONEBRAIN_AUTH_SECRET must fail closed here.
    errors = validate_bundle(_full_bundle(ONEBRAIN_AUTH_SECRET="short"))
    assert any("ONEBRAIN_AUTH_SECRET" in e and "at least 32" in e for e in errors)
    # Exactly 32 chars clears the floor.
    assert validate_bundle(_full_bundle(ONEBRAIN_AUTH_SECRET="x" * 32)) == []


def test_backup_s3_keys_are_optional_and_dotenv_ordered():
    # BK3: the two offsite-backup S3 credentials are OPTIONAL bundle keys (empty when backups
    # off), never REQUIRED, and render_dotenv emits them in canonical order when present.
    for k in ("ONEBRAIN_BACKUP_S3_ACCESS_KEY", "ONEBRAIN_BACKUP_S3_SECRET_KEY"):
        assert k in BUNDLE_KEYS and k in OPTIONAL_KEYS and k not in REQUIRED_KEYS
    # a valid bundle WITHOUT them stays valid (backups off)
    assert validate_bundle(_full_bundle()) == []
    # present -> emitted, access before secret (canonical order)
    body = render_dotenv(_full_bundle(
        ONEBRAIN_BACKUP_S3_ACCESS_KEY="AK", ONEBRAIN_BACKUP_S3_SECRET_KEY="SK"))
    assert "ONEBRAIN_BACKUP_S3_ACCESS_KEY=AK" in body and "ONEBRAIN_BACKUP_S3_SECRET_KEY=SK" in body
    assert body.index("ONEBRAIN_BACKUP_S3_ACCESS_KEY") < body.index("ONEBRAIN_BACKUP_S3_SECRET_KEY")


def test_runtime_bundle_secret_backfill_is_idempotent_and_never_replaces_existing_values():
    legacy = _full_bundle()
    for key in RUNTIME_DB_PASSWORD_KEYS:
        legacy.pop(key)
    legacy.pop("ONEBRAIN_LOGIN_RATE_LIMIT_SECRET")

    values = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32))
    updated, added = backfill_runtime_db_passwords(legacy, password_factory=lambda: next(values))
    assert added == RUNTIME_BUNDLE_BACKFILL_KEYS
    assert updated["POSTGRES_APP_PASSWORD"] == "a" * 32
    assert updated["POSTGRES_WORKER_PASSWORD"] == "b" * 32
    assert updated["POSTGRES_ASSISTANT_PASSWORD"] == "c" * 32
    assert updated["POSTGRES_COMMUNICATION_PASSWORD"] == "d" * 32
    assert updated["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"] == "e" * 32

    retried, added_again = backfill_runtime_db_passwords(
        updated, password_factory=lambda: (_ for _ in ()).throw(AssertionError("must not mint")))
    assert added_again == ()
    assert retried == updated


def test_runtime_bundle_secret_backfill_adds_only_missing_rate_limit_secret():
    legacy = _full_bundle()
    expected_passwords = {key: legacy[key] for key in RUNTIME_DB_PASSWORD_KEYS}
    legacy.pop("ONEBRAIN_LOGIN_RATE_LIMIT_SECRET")

    updated, added = backfill_runtime_db_passwords(
        legacy, password_factory=lambda: "rate-limit-secret-" + ("x" * 32))

    assert added == ("ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",)
    assert updated["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"] == "rate-limit-secret-" + ("x" * 32)
    assert {key: updated[key] for key in RUNTIME_DB_PASSWORD_KEYS} == expected_passwords


def test_runtime_bundle_secret_backfill_preserves_existing_rate_limit_secret():
    existing_secret = "existing-rate-limit-secret-" + ("x" * 32)
    legacy = _full_bundle(ONEBRAIN_LOGIN_RATE_LIMIT_SECRET=existing_secret)
    legacy.pop("POSTGRES_APP_PASSWORD")

    updated, added = backfill_runtime_db_passwords(
        legacy, password_factory=lambda: "app-password-" + ("y" * 32))

    assert added == ("POSTGRES_APP_PASSWORD",)
    assert updated["POSTGRES_APP_PASSWORD"] == "app-password-" + ("y" * 32)
    assert updated["ONEBRAIN_LOGIN_RATE_LIMIT_SECRET"] == existing_secret


def test_runtime_bundle_secret_backfill_replaces_weak_nonempty_role_and_rate_secrets():
    legacy = _full_bundle(
        POSTGRES_APP_PASSWORD="weak-app",
        POSTGRES_WORKER_PASSWORD="weak-worker",
        POSTGRES_ASSISTANT_PASSWORD="weak-assistant",
        POSTGRES_COMMUNICATION_PASSWORD="weak-communication",
        ONEBRAIN_LOGIN_RATE_LIMIT_SECRET="weak-rate-limit",
    )
    replacements = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32))

    updated, changed = backfill_runtime_db_passwords(
        legacy, password_factory=lambda: next(replacements))

    assert changed == RUNTIME_BUNDLE_BACKFILL_KEYS
    for key in RUNTIME_BUNDLE_BACKFILL_KEYS:
        assert len(updated[key]) >= 32

"""The pure secret-bundle contract (P5-00 · §1.5)."""

from __future__ import annotations

from app.fleet.bootstrap_bundle import (
    BUNDLE_KEYS,
    OPTIONAL_KEYS,
    REQUIRED_KEYS,
    render_dotenv,
    validate_bundle,
)


def _full_bundle(**overrides) -> dict:
    bundle = {
        "POSTGRES_PASSWORD": "pg-secret",
        "REDIS_PASSWORD": "redis-secret",
        "ONEBRAIN_FLEET_KEY": "fk_abc_def",
        "ONEBRAIN_LLM_API_KEY": "",
        "ONEBRAIN_AUTH_SECRET": "a" * 64,   # >= MIN_KEY_LENGTHS floor (32); token_hex(32) in prod
        "ONEBRAIN_ADMIN_EMAIL": "owner@example.com",
        "ONEBRAIN_ADMIN_PASSWORD": "owner-otp",
        "ONEBRAIN_SERVICE_KEY": "",
        "ONEBRAIN_SPACE_ID": "",
        "UPDATE_BACKUP_KEY": "backup-secret",
        "UPDATE_DESIRED_STATE_PUBLIC_KEYS": "pub1,pub2",
        "ONEBRAIN_DNS_TOKEN": "",
    }
    bundle.update(overrides)
    return bundle


def test_callback_token_is_never_a_bundle_key():
    # G1-7: the callback token stays baked in box.env, NEVER in the exchange bundle.
    assert "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN" not in BUNDLE_KEYS
    assert "ONEBRAIN_BOOTSTRAP_TOKEN" not in BUNDLE_KEYS


def test_required_and_optional_partition_bundle_keys():
    assert set(REQUIRED_KEYS) | set(OPTIONAL_KEYS) == set(BUNDLE_KEYS)
    assert set(REQUIRED_KEYS) & set(OPTIONAL_KEYS) == set()
    # The two explicitly-optional keys named in the spec.
    assert "ONEBRAIN_DNS_TOKEN" in OPTIONAL_KEYS
    assert "ONEBRAIN_SPACE_ID" in OPTIONAL_KEYS


def test_render_dotenv_emits_only_present_keys_in_canonical_order_lf():
    dotenv = render_dotenv(_full_bundle())
    # LF only, no CR.
    assert "\r" not in dotenv
    lines = dotenv.splitlines()
    # Every bundle key present -> one line each, in BUNDLE_KEYS order.
    assert [line.split("=", 1)[0] for line in lines] == list(BUNDLE_KEYS)
    assert "POSTGRES_PASSWORD=pg-secret" in lines
    assert "UPDATE_DESIRED_STATE_PUBLIC_KEYS=pub1,pub2" in lines
    # Present-but-empty keys are emitted as bare KEY= (no quoting).
    assert "ONEBRAIN_DNS_TOKEN=" in lines
    # Trailing newline (standard dotenv).
    assert dotenv.endswith("\n")


def test_render_dotenv_skips_absent_keys_and_ignores_extras():
    bundle = {"POSTGRES_PASSWORD": "x", "REDIS_PASSWORD": "y", "NOT_A_BUNDLE_KEY": "z"}
    dotenv = render_dotenv(bundle)
    assert dotenv == "POSTGRES_PASSWORD=x\nREDIS_PASSWORD=y\n"
    assert "NOT_A_BUNDLE_KEY" not in dotenv


def test_render_dotenv_does_not_quote_secret_values():
    dotenv = render_dotenv(_full_bundle(POSTGRES_PASSWORD="a b/c=+d"))
    assert "POSTGRES_PASSWORD=a b/c=+d\n" in dotenv


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


def test_validate_bundle_flags_weak_auth_secret_below_floor():
    # Present-but-too-short is as fatal as missing: app/main.py's >=32 guard would boot-loop
    # the box, so a <32-char ONEBRAIN_AUTH_SECRET must fail closed here.
    errors = validate_bundle(_full_bundle(ONEBRAIN_AUTH_SECRET="short"))
    assert any("ONEBRAIN_AUTH_SECRET" in e and "at least 32" in e for e in errors)
    # Exactly 32 chars clears the floor.
    assert validate_bundle(_full_bundle(ONEBRAIN_AUTH_SECRET="x" * 32)) == []

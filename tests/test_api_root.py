"""API root behavior after the Next.js UI cutover."""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _load_main(monkeypatch, **env):
    base_env = {
        "ONEBRAIN_AUTH_SECRET": "test-secret-test-secret-test-secret",
        "ONEBRAIN_SEED_SAMPLE_DATA": "false",
        "ONEBRAIN_SEED_DEMO_USERS": "false",
        "ONEBRAIN_COOKIE_SECURE": "false",
    }
    for key, value in {**base_env, **env}.items():
        monkeypatch.setenv(key, value)

    import app.config as config

    config.get_settings.cache_clear()
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    config.get_settings.cache_clear()
    return module


def test_api_root_is_status_json_by_default(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "onebrain-api",
        "status": "ok",
        "ui": "nextjs",
        "docs": "/docs",
        "health": "/health",
    }


def test_api_root_redirects_to_nextjs_when_configured(monkeypatch):
    main = _load_main(monkeypatch, ONEBRAIN_ADMIN_UI_URL="https://onebrain.example.com")
    client = TestClient(main.create_app())

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://onebrain.example.com"


def test_legacy_static_ui_is_disabled_unless_explicitly_enabled(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 404


def test_legacy_static_ui_can_be_enabled_for_local_debugging(monkeypatch):
    main = _load_main(monkeypatch, ONEBRAIN_LEGACY_STATIC_UI_ENABLED="true")
    client = TestClient(main.create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 200
    assert "/static/js/main.js" in response.text


# --- P5-02: G1-1 startup interlock (operator_mode) ---------------------------

def _import_main(monkeypatch, **env):
    """Import app.main fresh with env applied. app.main runs create_app() at module
    load, so a G1-1-excluding config raises HERE (fail-fast). Caller cleans up."""
    base_env = {
        "ONEBRAIN_AUTH_SECRET": "test-secret-test-secret-test-secret",
        "ONEBRAIN_SEED_SAMPLE_DATA": "false",
        "ONEBRAIN_SEED_DEMO_USERS": "false",
        "ONEBRAIN_COOKIE_SECURE": "false",
    }
    for key, value in {**base_env, **env}.items():
        monkeypatch.setenv(key, value)
    import app.config as config
    config.get_settings.cache_clear()
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _clear_main():
    import app.config as config
    sys.modules.pop("app.main", None)
    config.get_settings.cache_clear()


def test_operator_startup_raises_when_active_signer_excluded(monkeypatch):
    from app.trust.signing import generate_keypair
    priv, _pub = generate_keypair()
    _op, other_pub = generate_keypair()
    try:
        with pytest.raises(RuntimeError, match="not in the served"):
            _import_main(
                monkeypatch,
                ONEBRAIN_OPERATOR_MODE="true",
                ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=priv,
                ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS=other_pub,   # EXCLUDES the active signer
            )
    finally:
        _clear_main()


def test_operator_startup_passes_when_active_signer_present(monkeypatch):
    from app.trust.signing import generate_keypair
    priv, pub = generate_keypair()
    _op, other_pub = generate_keypair()
    try:
        main = _import_main(
            monkeypatch,
            ONEBRAIN_OPERATOR_MODE="true",
            ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY=priv,
            ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS=f"{other_pub},{pub}",  # INCLUDES the signer
        )
        assert main.app is not None
    finally:
        _clear_main()


def test_operator_startup_skipped_when_emission_off(monkeypatch):
    # operator_mode with NO private key -> nothing to sign, nothing to brick -> boots.
    try:
        main = _import_main(monkeypatch, ONEBRAIN_OPERATOR_MODE="true")
        assert main.app is not None
    finally:
        _clear_main()


def test_production_mission_control_preflight_runs_at_startup(monkeypatch):
    """Production-like MC must fail before routers or background daemons start."""
    try:
        with pytest.raises(RuntimeError, match="ONEBRAIN_PROVISIONER_BACKEND=hetzner"):
            _import_main(
                monkeypatch,
                ONEBRAIN_ENVIRONMENT="production",
                ONEBRAIN_VECTOR_STORE="pgvector",
                ONEBRAIN_DATABASE_URL="postgresql://onebrain:secret@postgres/onebrain",
                ONEBRAIN_RLS_ENFORCED="true",
                ONEBRAIN_OPERATOR_MODE="true",
                ONEBRAIN_POSTGRES_APP_ROLE="onebrain_app",
                ONEBRAIN_POSTGRES_WORKER_ROLE="onebrain_worker",
                ONEBRAIN_LOGIN_RATE_LIMIT_SECRET="x" * 32,
                ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS="mc.example",
            )
    finally:
        _clear_main()

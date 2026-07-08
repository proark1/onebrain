"""API root behavior after the Next.js UI cutover."""

from __future__ import annotations

import importlib
import sys

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

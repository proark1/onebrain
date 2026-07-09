from __future__ import annotations

import json
from urllib.request import urlopen

import pytest

from app.workers.health import start_worker_health_server_if_configured


def test_worker_health_server_stays_off_without_port_in_auto_mode(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("ONEBRAIN_WORKER_HEALTH_SERVER", raising=False)

    assert start_worker_health_server_if_configured() is None


def test_worker_health_server_serves_health_when_port_is_set(monkeypatch):
    monkeypatch.setenv("PORT", "0")
    monkeypatch.delenv("ONEBRAIN_WORKER_HEALTH_SERVER", raising=False)
    server = start_worker_health_server_if_configured()
    assert server is not None

    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read().decode("utf-8")) == {
                "status": "ok",
                "process": "worker",
            }
    finally:
        server.shutdown()
        server.server_close()


def test_worker_health_server_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PORT", "0")
    monkeypatch.setenv("ONEBRAIN_WORKER_HEALTH_SERVER", "false")

    assert start_worker_health_server_if_configured() is None


def test_worker_health_server_rejects_invalid_port(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")
    monkeypatch.setenv("ONEBRAIN_WORKER_HEALTH_SERVER", "true")

    with pytest.raises(RuntimeError, match="PORT"):
        start_worker_health_server_if_configured()

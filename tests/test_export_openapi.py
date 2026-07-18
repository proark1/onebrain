from __future__ import annotations

import json
import os

from scripts import export_openapi


def test_check_mode_detects_missing_and_stale_schema(monkeypatch, tmp_path):
    target = tmp_path / "openapi.json"
    surfaces: list[str] = []
    monkeypatch.setattr(
        export_openapi,
        "_schema_json",
        lambda surface: surfaces.append(surface) or '{"openapi":"3.1.0"}\n',
    )

    assert export_openapi.main([str(target), "--check"]) == 1

    target.write_text('{"openapi":"3.0.0"}\n', encoding="utf-8")
    assert export_openapi.main([str(target), "--check"]) == 1

    target.write_text('{"openapi":"3.1.0"}\n', encoding="utf-8")
    assert export_openapi.main([
        str(target), "--check", "--surface", "customer",
    ]) == 0
    assert surfaces == ["operator", "operator", "customer"]


def test_operator_and_customer_openapi_exports_gate_their_deployment_routes(
    monkeypatch,
):
    sentinel = "https://fleet.example.test"
    monkeypatch.setenv("ONEBRAIN_FLEET_URL", sentinel)
    monkeypatch.delenv("ONEBRAIN_FLEET_REPORTER_ENABLED", raising=False)
    operator_paths = json.loads(export_openapi._schema_json("operator"))["paths"]
    customer_paths = json.loads(export_openapi._schema_json("customer"))["paths"]

    assert "/api/drive/bootstrap" not in operator_paths
    assert "/api/drive/bootstrap" in customer_paths
    assert "/api/operator/deployments" in operator_paths
    assert "/api/operator/deployments" not in customer_paths
    assert "/api/fleet/overview" in operator_paths
    assert "/api/fleet/overview" not in customer_paths
    assert os.environ["ONEBRAIN_FLEET_URL"] == sentinel
    assert "ONEBRAIN_FLEET_REPORTER_ENABLED" not in os.environ

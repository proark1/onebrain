from __future__ import annotations

from scripts import export_openapi


def test_check_mode_detects_missing_and_stale_schema(monkeypatch, tmp_path):
    target = tmp_path / "openapi.json"
    monkeypatch.setattr(export_openapi, "_schema_json", lambda: '{"openapi":"3.1.0"}\n')

    assert export_openapi.main([str(target), "--check"]) == 1

    target.write_text('{"openapi":"3.0.0"}\n', encoding="utf-8")
    assert export_openapi.main([str(target), "--check"]) == 1

    target.write_text('{"openapi":"3.1.0"}\n', encoding="utf-8")
    assert export_openapi.main([str(target), "--check"]) == 0

"""The host-only development-gate reporter contract.

The reporter intentionally has no application imports: it runs as root on the
box, reads only local Docker/update metadata, and posts the closed fleet.v2
schema. These tests keep it deterministic without Docker or Mission Control.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError


_REPORT = Path(__file__).parents[1] / "deploy" / "box" / "onebrain_gate_report.py"
_MODULES = (
    "onebrain-api", "onebrain-admin-ui", "onebrain-workers", "assistant-service",
    "communication-api", "communication-widget", "communication-voice", "communication-workers",
)


def _load_report():
    spec = importlib.util.spec_from_file_location("onebrain_gate_report", _REPORT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(tmp_path) -> dict[str, str]:
    release = {
        "version": "2026.07.13.1",
        "migration_to": "0022_release_promotion_gate",
        "modules": {module_id: f"{module_id}-v1" for module_id in _MODULES},
    }
    initial = tmp_path / "installed-release.json"
    initial.write_text(json.dumps(release), encoding="utf-8")
    maintenance = tmp_path / "onebrain-maintenance"
    work = maintenance / "onebrain_update"
    work.mkdir(parents=True)
    (work / "update_state.json").write_text(json.dumps({
        "last_target_version": release["version"],
        "outcome": "succeeded",
        "migration_reached": release["migration_to"],
        "attempt_id": "rollout_123",
        "ts": "2026-07-13T10:00:00+00:00",
        "backup_status": "success",
        "backup_ts": "2026-07-13T09:59:00+00:00",
        "backup_manifest": "sha256:" + "a" * 64 + ":42",
        "ignored_untrusted_text": "never reported",
    }), encoding="utf-8")
    (work / "secrets_epoch").write_text("7\n", encoding="utf-8")
    return {
        "ONEBRAIN_DEPLOYMENT_ID": "onebrain-development-next",
        "ONEBRAIN_FLEET_URL": "https://mc.example.com",
        "ONEBRAIN_FLEET_KEY": "fk_sensitive",
        "ONEBRAIN_DATA_MOUNT": str(tmp_path),
        "ONEBRAIN_MAINTENANCE_DIR": str(maintenance),
        "UPDATE_INITIAL_RELEASE_FILE": str(initial),
        "UPDATE_LOCAL_MODULES": ",".join(_MODULES),
        "UPDATE_COMPOSE_DIR": "/opt/onebrain",
        "UPDATE_COMPOSE_PROJECT": "onebrain-development-next",
        "DOCKER": "docker",
    }


def _healthy_runner(args):
    if "ps" in args:
        return SimpleNamespace(returncode=0, stdout=f"container-{args[-1]}\n")
    if "inspect" in args:
        return SimpleNamespace(returncode=0, stdout="running\n")
    if "exec" in args:
        return SimpleNamespace(returncode=0, stdout="0022_release_promotion_gate (head)\n")
    raise AssertionError(f"unexpected command: {args!r}")


def _verified_volume(_env) -> bool:
    return True


def test_host_reporter_builds_closed_full_stack_metadata_only_heartbeat(tmp_path):
    report = _load_report()
    observed_paths = []

    def storage_collector(path):
        observed_paths.append(path)
        return {"total_bytes": 1000, "available_bytes": 200 if path == "/" else 300}

    heartbeat = report.build_heartbeat(
        _env(tmp_path), runner=_healthy_runner, health_probe=lambda _url: True,
        storage_collector=storage_collector,
        maintenance_volume_verifier=_verified_volume,
    )

    assert heartbeat["contract_version"] == "fleet.v2"
    assert heartbeat["deployment_id"] == "onebrain-development-next"
    assert heartbeat["onebrain"] == {
        "version": "2026.07.13.1",
        "migration_revision": "0022_release_promotion_gate",
        "healthy": True,
        "chunks": 0,
        "intake_records": 0,
        "users": 0,
        "accounts": 0,
        "active_service_keys": 0,
        "jobs_pending": 0,
        "jobs_failed": 0,
        "auth_failures_recent": 0,
            "api_5xx_recent": 0,
            "uptime_seconds": 0,
            "user_management_v1": True,
        }
    assert {row["module_id"]: row["version"] for row in heartbeat["modules"]} == {
        module_id: f"{module_id}-v1" for module_id in _MODULES
    }
    assert all(row["healthy"] for row in heartbeat["modules"])
    assert heartbeat["update"]["attempt_id"] == "rollout_123"
    assert heartbeat["update"]["applied_secrets_epoch"] == 7
    assert heartbeat["update"]["backup_manifest"].endswith(":42")
    assert heartbeat["storage"] == {
        "root": {"total_bytes": 1000, "available_bytes": 200},
        "data": {"total_bytes": 1000, "available_bytes": 300},
        "data_volume_unavailable": False,
    }
    assert observed_paths == ["/", str(tmp_path)]
    encoded = json.dumps(heartbeat)
    for forbidden in ("fk_sensitive", "mc.example.com", "ignored_untrusted_text", "never reported"):
        assert forbidden not in encoded


def test_host_reporter_uses_verified_last_applied_images_as_release_modules(tmp_path):
    report = _load_report()
    env = _env(tmp_path)
    version = "2026.07.17.172"
    work = tmp_path / "onebrain-maintenance" / "onebrain_update"
    (work / "last_applied.json").write_text(json.dumps({
        "version": version,
        "images": {
            "onebrain-api": "ghcr.io/example/api@sha256:" + "a" * 64,
            "onebrain-admin-ui": "ghcr.io/example/ui@sha256:" + "b" * 64,
            "onebrain-workers": "ghcr.io/example/workers@sha256:" + "c" * 64,
        },
    }), encoding="utf-8")

    heartbeat = report.build_heartbeat(
        env, runner=_healthy_runner, health_probe=lambda _url: True,
        maintenance_volume_verifier=_verified_volume,
    )

    assert heartbeat["onebrain"]["version"] == version
    assert heartbeat["onebrain"]["healthy"] is True
    assert {row["module_id"]: row["version"] for row in heartbeat["modules"]} == {
        "onebrain-api": version,
        "onebrain-admin-ui": version,
        "onebrain-workers": version,
        "assistant-service": "",
        "communication-api": "",
        "communication-widget": "",
        "communication-voice": "",
        "communication-workers": "",
    }


def test_host_reporter_hides_update_state_when_data_volume_verification_fails(tmp_path):
    report = _load_report()
    observed_paths = []

    def storage_collector(path):
        observed_paths.append(path)
        return {"total_bytes": 1000, "available_bytes": 200}

    heartbeat = report.build_heartbeat(
        _env(tmp_path), runner=_healthy_runner, health_probe=lambda _url: True,
        storage_collector=storage_collector,
        maintenance_volume_verifier=lambda _env: False,
    )

    assert heartbeat["onebrain"]["version"] == ""
    assert heartbeat["onebrain"]["healthy"] is False
    assert heartbeat["update"]["outcome"] == "none"
    assert heartbeat["storage"]["data"] == {"total_bytes": 0, "available_bytes": 0}
    assert heartbeat["storage"]["data_volume_unavailable"] is True
    assert observed_paths == ["/"]


def test_host_reporter_health_probe_matches_updater_curl(monkeypatch):
    report = _load_report()
    monkeypatch.setattr(report, "_run", lambda args: SimpleNamespace(
        returncode=0 if args == ["curl", "-sf", "http://127.0.0.1/health"] else 22,
    ))

    assert report._http_healthy("http://127.0.0.1/health") is True
    assert report._http_healthy("http://127.0.0.1/missing") is False


def test_host_reporter_fails_closed_for_missing_migration_or_module(tmp_path):
    report = _load_report()

    def degraded_runner(args):
        if "ps" in args:
            return SimpleNamespace(returncode=0, stdout=f"container-{args[-1]}\n")
        if "inspect" in args:
            return SimpleNamespace(returncode=0, stdout="exited\n")
        if "exec" in args:
            return SimpleNamespace(returncode=1, stdout="")
        raise AssertionError(f"unexpected command: {args!r}")

    heartbeat = report.build_heartbeat(
        _env(tmp_path), runner=degraded_runner, health_probe=lambda _url: False,
        maintenance_volume_verifier=_verified_volume,
    )

    assert heartbeat["onebrain"]["healthy"] is False
    assert heartbeat["onebrain"]["migration_revision"] == ""
    assert not any(row["healthy"] for row in heartbeat["modules"])


def test_host_reporter_retries_without_storage_for_legacy_closed_schema(tmp_path, monkeypatch):
    """An additive storage report must not silence a legacy MC heartbeat path."""
    report = _load_report()
    env = _env(tmp_path)
    heartbeat = report.build_heartbeat(
        env, runner=_healthy_runner, health_probe=lambda _url: True,
        maintenance_volume_verifier=_verified_volume,
    )
    bodies = []

    class Response:
        status = 204

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        assert timeout == 10
        bodies.append(json.loads(request.data.decode("utf-8")))
        if len(bodies) == 1:
            raise HTTPError(request.full_url, 422, "closed schema", hdrs=None, fp=None)
        return Response()

    monkeypatch.setattr(report.urllib.request, "urlopen", urlopen)

    assert report.send_heartbeat(env, heartbeat) is True
    assert "storage" in bodies[0]
    assert "storage" not in bodies[1]

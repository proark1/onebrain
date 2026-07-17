#!/usr/bin/env python3
"""Root-only, metadata-only fleet.v2 reporter for customer-shaped boxes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


MODULE_IDS = (
    "onebrain-api",
    "onebrain-admin-ui",
    "onebrain-workers",
    "assistant-service",
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
)
UPDATE_OUTCOMES = {"none", "in_progress", "succeeded", "failed", "rolled_back"}
_SAFE_STRING = re.compile(r"^[^\r\n]{0,128}$")
_BACKUP_MANIFEST = re.compile(r"^sha256:[0-9a-f]{64}:[0-9]+$")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_string(value: object, *, limit: int = 64) -> str:
    if not isinstance(value, str) or len(value) > limit or not _SAFE_STRING.fullmatch(value):
        return ""
    return value


def _module_ids(env: Mapping[str, str]) -> list[str]:
    listed = (env.get("UPDATE_LOCAL_MODULES") or "").split(",")
    return [module_id for module_id in MODULE_IDS if module_id in {item.strip() for item in listed}]


def _release(env: Mapping[str, str]) -> dict:
    """Prefer verified updater state, then the provision-time descriptor."""
    data_dir = Path(env.get("UPDATE_DATA_DIR") or "/data")
    candidates = [
        data_dir / "onebrain_update" / "last_applied.json",
        Path(env.get("UPDATE_INITIAL_RELEASE_FILE") or "/opt/onebrain/installed-release.json"),
    ]
    for path in candidates:
        release = _read_json(path)
        version = _safe_string(release.get("version"))
        modules = release.get("modules")
        if not isinstance(modules, dict) and isinstance(release.get("images"), dict):
            modules = dict.fromkeys(release["images"], version)
        if version and isinstance(modules, dict):
            return {**release, "modules": modules}
    return {}


def _update_state(env: Mapping[str, str]) -> dict:
    raw = _read_json(Path(env.get("UPDATE_DATA_DIR") or "/data") / "onebrain_update" / "update_state.json")
    outcome = raw.get("outcome") if raw.get("outcome") in UPDATE_OUTCOMES else "none"
    backup_manifest = _safe_string(raw.get("backup_manifest"), limit=128)
    return {
        "last_target_version": _safe_string(raw.get("last_target_version")),
        "outcome": outcome,
        "migration_reached": _safe_string(raw.get("migration_reached")),
        "attempt_id": _safe_string(raw.get("attempt_id")),
        "ts": _safe_string(raw.get("ts"), limit=40),
        "backup_status": raw.get("backup_status") if raw.get("backup_status") in {"", "success", "failed"} else "",
        "backup_ts": _safe_string(raw.get("backup_ts"), limit=40),
        "backup_manifest": backup_manifest if _BACKUP_MANIFEST.fullmatch(backup_manifest) else "",
    }


def _run(args: list[str]):
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None


def _compose_args(env: Mapping[str, str]) -> list[str]:
    return [
        (env.get("DOCKER") or "docker").strip(),
        "compose",
        "--project-name", (env.get("UPDATE_COMPOSE_PROJECT") or "onebrain").strip(),
        "-f", (env.get("UPDATE_COMPOSE_DIR") or "/opt/onebrain").rstrip("/") + "/docker-compose.yml",
    ]


def _container_healthy(env: Mapping[str, str], module_id: str, runner: Callable) -> bool:
    """A missing, stopped, unhealthy, or ambiguously duplicated service is false.

    Docker's health state wins when an image provides one; otherwise a running
    state is the strongest universally available local signal. The OneBrain API
    also receives an HTTP probe in ``build_heartbeat``.
    """
    listed = runner(_compose_args(env) + ["ps", "-q", module_id])
    if not listed or getattr(listed, "returncode", 1) != 0:
        return False
    ids = str(getattr(listed, "stdout", "")).split()
    if len(ids) != 1:
        return False
    inspected = runner([
        (env.get("DOCKER") or "docker").strip(),
        "inspect",
        "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
        ids[0],
    ])
    if not inspected or getattr(inspected, "returncode", 1) != 0:
        return False
    return str(getattr(inspected, "stdout", "")).strip().lower() in {"healthy", "running"}


def _migration_revision(env: Mapping[str, str], runner: Callable) -> tuple[str, bool]:
    current = runner(_compose_args(env) + ["exec", "-T", "onebrain-api", "alembic", "current"])
    if not current or getattr(current, "returncode", 1) != 0:
        return "", False
    revision = str(getattr(current, "stdout", "")).strip().split(maxsplit=1)
    value = _safe_string(revision[0] if revision else "")
    return value, bool(value)


def _http_healthy(url: str) -> bool:
    result = _run(["curl", "-sf", url])
    return bool(result and result.returncode == 0)


def build_heartbeat(
    env: Mapping[str, str], *, runner: Callable = _run, health_probe: Callable[[str], bool] = _http_healthy,
) -> dict:
    """Build the closed fleet.v2 body from local deployment metadata only."""
    release = _release(env)
    module_ids = _module_ids(env)
    versions = release.get("modules") if isinstance(release.get("modules"), dict) else {}
    module_rows = []
    module_health: dict[str, bool] = {}
    for module_id in module_ids:
        healthy = _container_healthy(env, module_id, runner)
        module_health[module_id] = healthy
        module_rows.append({
            "module_id": module_id,
            "version": _safe_string(versions.get(module_id)),
            "healthy": healthy,
            "events_pending": 0,
            "events_failed": 0,
        })

    migration, migration_ok = _migration_revision(env, runner)
    api_probe_ok = health_probe(env.get("UPDATE_HEALTH_URL") or "http://127.0.0.1/health")
    release_version = _safe_string(release.get("version"))
    onebrain_healthy = bool(
        release_version
        and migration_ok
        and module_health.get("onebrain-api", False)
        and api_probe_ok
    )
    return {
        "contract_version": "fleet.v2",
        "deployment_id": _safe_string(env.get("ONEBRAIN_DEPLOYMENT_ID"), limit=120),
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "onebrain": {
            "version": release_version,
            "migration_revision": migration,
            "healthy": onebrain_healthy,
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
        },
        "modules": module_rows,
        "update": _update_state(env),
    }


def send_heartbeat(env: Mapping[str, str], heartbeat: dict) -> bool:
    """Post without logging secret-bearing URLs or headers on failure."""
    fleet_url = (env.get("ONEBRAIN_FLEET_URL") or "").rstrip("/")
    fleet_key = env.get("ONEBRAIN_FLEET_KEY") or ""
    if not fleet_url or not fleet_key or not heartbeat["deployment_id"]:
        return False
    request = urllib.request.Request(
        fleet_url + "/api/fleet/heartbeat",
        data=json.dumps(heartbeat, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {fleet_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (getattr(response, "status", 0) or response.getcode()) < 400
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    heartbeat = build_heartbeat(os.environ)
    if "--print" in args:
        print(json.dumps(heartbeat, sort_keys=True))
        return 0
    send_heartbeat(os.environ, heartbeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

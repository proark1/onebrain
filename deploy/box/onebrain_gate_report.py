#!/usr/bin/env python3
"""Root-only, metadata-only fleet.v2 reporter for customer-shaped boxes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
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


def _data_volume_verified(env: Mapping[str, str]) -> bool:
    """Run the same UUID/mount verifier used before Docker starts."""
    script = (env.get("ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT") or
              "/opt/onebrain/onebrain-data-volume.sh").strip()
    if not script:
        return False
    child_env = dict(os.environ)
    child_env.update({key: value for key, value in env.items() if isinstance(value, str)})
    try:
        result = subprocess.run(
            [script, "verify"], env=child_env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _maintenance_dir(
    env: Mapping[str, str], *, volume_verifier: Callable[[Mapping[str, str]], bool] = _data_volume_verified,
) -> Path | None:
    """Return only the dedicated state directory on a verified data volume."""
    mount = Path(env.get("ONEBRAIN_DATA_MOUNT") or env.get("ONEBRAIN_DATA_VOLUME_PATH") or
                 "/mnt/onebrain-data")
    directory = Path(env.get("ONEBRAIN_MAINTENANCE_DIR") or mount / "onebrain-maintenance")
    if not mount.is_absolute() or directory != mount / "onebrain-maintenance":
        return None
    if not volume_verifier(env):
        return None
    return directory


def _release(env: Mapping[str, str], maintenance_dir: Path | None) -> dict:
    """Prefer verified updater state, then the provision-time descriptor."""
    if maintenance_dir is None:
        return {}
    candidates = [
        maintenance_dir / "onebrain_update" / "last_applied.json",
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


def _update_state(maintenance_dir: Path | None) -> dict:
    raw = _read_json(maintenance_dir / "onebrain_update" / "update_state.json") \
        if maintenance_dir is not None else {}
    outcome = raw.get("outcome") if raw.get("outcome") in UPDATE_OUTCOMES else "none"
    backup_manifest = _safe_string(raw.get("backup_manifest"), limit=128)
    try:
        epoch = max(0, int((maintenance_dir / "onebrain_update/secrets_epoch").read_text()))
    except Exception:
        epoch = 0
    return {
        "last_target_version": _safe_string(raw.get("last_target_version")),
        "outcome": outcome,
        "migration_reached": _safe_string(raw.get("migration_reached")),
        "attempt_id": _safe_string(raw.get("attempt_id")),
        "ts": _safe_string(raw.get("ts"), limit=40),
        "backup_status": raw.get("backup_status") if raw.get("backup_status") in {"", "success", "failed"} else "",
        "backup_ts": _safe_string(raw.get("backup_ts"), limit=40),
        "backup_manifest": backup_manifest if _BACKUP_MANIFEST.fullmatch(backup_manifest) else "",
        "applied_secrets_epoch": epoch,
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


def _storage_capacity(path: str) -> dict[str, int]:
    """Report only usable capacity; an unmounted/unobservable volume is ``0/0``.

    ``f_bavail`` deliberately reflects blocks available to the non-root database
    process rather than root-reserved ext4 blocks. No pathname is emitted in the
    heartbeat, so this remains metadata-only.
    """
    try:
        stat = os.statvfs(path)
        block_size = int(getattr(stat, "f_frsize", 0) or getattr(stat, "f_bsize", 0))
        total = max(0, int(stat.f_blocks) * block_size)
        available = max(0, int(stat.f_bavail) * block_size)
        if total <= 0 or available > total:
            return {"total_bytes": 0, "available_bytes": 0}
        return {"total_bytes": total, "available_bytes": available}
    except (AttributeError, OSError, TypeError, ValueError):
        return {"total_bytes": 0, "available_bytes": 0}


def build_heartbeat(
    env: Mapping[str, str], *, runner: Callable = _run, health_probe: Callable[[str], bool] = _http_healthy,
    storage_collector: Callable[[str], dict[str, int]] = _storage_capacity,
    maintenance_volume_verifier: Callable[[Mapping[str, str]], bool] = _data_volume_verified,
) -> dict:
    """Build the closed fleet.v2 body from local deployment metadata only."""
    maintenance_dir = _maintenance_dir(env, volume_verifier=maintenance_volume_verifier)
    release = _release(env, maintenance_dir)
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
    root_capacity = storage_collector("/")
    data_volume_path = (env.get("ONEBRAIN_DATA_MOUNT") or
                        env.get("ONEBRAIN_DATA_VOLUME_PATH") or "/mnt/onebrain-data")
    # The same verifier that authorizes updater state also controls capacity
    # telemetry. A bare mountpoint can be the root disk after a failed attach;
    # only the UUID-verified maintenance path may report durable-data space.
    data_volume_verified = maintenance_dir is not None
    data_capacity = (
        storage_collector(data_volume_path)
        if data_volume_verified
        else {"total_bytes": 0, "available_bytes": 0}
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
            "user_management_v1": True,
            "uptime_seconds": 0,
        },
        "modules": module_rows,
        "update": _update_state(maintenance_dir),
        "storage": {
            "root": root_capacity,
            "data": data_capacity,
            "data_volume_unavailable": not data_volume_verified,
        },
    }


def _post_heartbeat(fleet_url: str, fleet_key: str, heartbeat: dict) -> bool:
    """Post a closed heartbeat payload; callers choose their compatibility form."""
    request = urllib.request.Request(
        fleet_url + "/api/fleet/heartbeat",
        data=json.dumps(heartbeat, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {fleet_key}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return (getattr(response, "status", 0) or response.getcode()) < 400


def send_heartbeat(env: Mapping[str, str], heartbeat: dict) -> bool:
    """Post without logging secret-bearing URLs or headers on failure.

    The storage field was added to fleet.v2 after already-deployed Mission
    Control instances began enforcing a closed schema.  On their explicit 422
    rejection, retry exactly once without only that additive field.  This keeps
    health/update reporting alive during the ordered host-before-MC upgrade;
    all other failures remain fail-closed and are not retried.
    """
    fleet_url = (env.get("ONEBRAIN_FLEET_URL") or "").rstrip("/")
    fleet_key = env.get("ONEBRAIN_FLEET_KEY") or ""
    if not fleet_url or not fleet_key or not heartbeat["deployment_id"]:
        return False
    try:
        return _post_heartbeat(fleet_url, fleet_key, heartbeat)
    except urllib.error.HTTPError as error:
        if error.code != 422 or "storage" not in heartbeat:
            return False
        legacy_heartbeat = dict(heartbeat)
        legacy_heartbeat.pop("storage", None)
        try:
            return _post_heartbeat(fleet_url, fleet_key, legacy_heartbeat)
        except Exception:
            return False
    except Exception:
        return False


def provision_callback_payload(env: Mapping[str, str]) -> dict | None:
    kind = env.get("ONEBRAIN_CALLBACK_KIND") or "completion"
    if kind not in {"failure", "completion"}:
        return None
    payload = {
        "status": env.get("ONEBRAIN_CALLBACK_STATUS") or "",
        "smoke_status": env.get("ONEBRAIN_CALLBACK_SMOKE") or "",
    }
    if kind == "failure":
        payload["failure_reason"] = "metadata_egress_block_failed"
    else:
        payload["bootstrap_password"] = env.get("ONEBRAIN_ADMIN_PASSWORD") or ""
        payload["external_run_url"] = env.get("ONEBRAIN_CALLBACK_INSTANCE") or ""
    return payload


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--provision-callback" in args:
        payload = provision_callback_payload(os.environ)
        if payload is None:
            return 2
        print(json.dumps(payload, separators=(",", ":")))
        return 0
    heartbeat = build_heartbeat(os.environ)
    if "--print" in args:
        print(json.dumps(heartbeat, sort_keys=True))
        return 0
    send_heartbeat(os.environ, heartbeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

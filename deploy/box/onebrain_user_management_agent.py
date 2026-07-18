#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import urllib.request
import base64
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


MAX_BODY = 1_000_000
COMMAND_DOMAIN = b"onebrain:user-management-command:v1\x00"
ALLOWED_ACTIONS = {
    "directory.snapshot", "user.create", "user.password.reset",
    "user.disable", "user.enable", "user.delete",
}


def _valid_command(command: dict, deployment_id: str, public_keys: str) -> bool:
    try:
        signature = command["signature"]
        unsigned = dict(command)
        del unsigned["signature"]
        payload = COMMAND_DOMAIN + json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        if command.get("contract") != "user-management-command.v1":
            return False
        if command.get("deployment_id") != deployment_id or command.get("action") not in ALLOWED_ACTIONS:
            return False
        if str(command.get("expires_at") or "") <= datetime.now(timezone.utc).isoformat():
            return False
        for encoded in (key.strip() for key in public_keys.split(",") if key.strip()):
            try:
                key = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded, validate=True))
                key.verify(base64.b64decode(signature, validate=True), payload)
                return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _request(url: str, fleet_key: str, deployment_id: str, *, data: dict | None = None):
    encoded = json.dumps(data, separators=(",", ":")).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url,
        data=encoded,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {fleet_key}",
            "Content-Type": "application/json",
            "X-OneBrain-Deployment-Id": deployment_id,
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("oversized Mission Control response")
        return json.loads(body) if body else None


def _atomic_outbox(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _submit(base: str, fleet_key: str, deployment_id: str, job_id: str, value: dict) -> bool:
    try:
        if value.get("ok") is True:
            result = dict(value.get("result") or {})
            result["deployment_id"] = deployment_id
            _request(f"{base}/api/fleet/user-management/jobs/{job_id}/result", fleet_key, deployment_id, data=result)
        else:
            _request(
                f"{base}/api/fleet/user-management/jobs/{job_id}/failure",
                fleet_key,
                deployment_id,
                data={"deployment_id": deployment_id, "error_code": value.get("error_code", "internal_failure")},
            )
        return True
    except Exception:
        return False


def _run_cli(command: dict, deployment_id: str, public_keys: str) -> dict:
    compose_dir = (os.environ.get("UPDATE_COMPOSE_DIR") or "/opt/onebrain").rstrip("/")
    args = [
        (os.environ.get("DOCKER") or "docker").strip(),
        "compose",
        "--project-name", (os.environ.get("UPDATE_COMPOSE_PROJECT") or "onebrain").strip(),
        "-f", compose_dir + "/docker-compose.yml",
        "exec", "-T",
        "-e", f"ONEBRAIN_MANAGEMENT_DEPLOYMENT_ID={deployment_id}",
        "-e", f"ONEBRAIN_MANAGEMENT_PUBLIC_KEYS={public_keys}",
        "onebrain-api", "python", "-m", "app.user_management.cli",
    ]
    completed = subprocess.run(
        args,
        input=json.dumps(command, separators=(",", ":")),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_BODY:
        return {"ok": False, "error_code": "internal_failure"}
    try:
        value = json.loads(completed.stdout)
        return value if isinstance(value, dict) else {"ok": False, "error_code": "internal_failure"}
    except Exception:
        return {"ok": False, "error_code": "internal_failure"}


def run_once() -> bool:
    base = (os.environ.get("ONEBRAIN_FLEET_URL") or "").rstrip("/")
    fleet_key = os.environ.get("ONEBRAIN_FLEET_KEY") or ""
    deployment_id = os.environ.get("ONEBRAIN_DEPLOYMENT_ID") or ""
    public_keys = (
        os.environ.get("UPDATE_DESIRED_STATE_PUBLIC_KEYS")
        or os.environ.get("UPDATE_DESIRED_STATE_PUBLIC_KEY")
        or ""
    )
    if not base or not fleet_key or not deployment_id or not public_keys:
        return False
    outbox = Path(os.environ.get("ONEBRAIN_USER_MANAGEMENT_OUTBOX") or "/opt/onebrain/user-management-outbox")

    for pending in sorted(outbox.glob("*.json")) if outbox.exists() else []:
        try:
            value = json.loads(pending.read_text(encoding="utf-8"))
            if _submit(base, fleet_key, deployment_id, pending.stem, value):
                pending.unlink()
        except Exception:
            continue

    try:
        command = _request(
            base + "/api/fleet/user-management/jobs/next",
            fleet_key,
            deployment_id,
        )
    except Exception:
        return False
    if not command:
        return True
    if not _valid_command(command, deployment_id, public_keys):
        return False
    job_id = str(command.get("command_id") or "")
    if not job_id:
        return False
    value = _run_cli(command, deployment_id, public_keys)
    path = outbox / f"{job_id}.json"
    _atomic_outbox(path, value)
    if _submit(base, fleet_key, deployment_id, job_id, value):
        path.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    raise SystemExit(0 if run_once() else 1)

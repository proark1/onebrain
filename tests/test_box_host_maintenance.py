"""Focused safety coverage for the root-only host-maintenance timer.

The functional tests run the real shell script against a tiny Docker CLI stub.
They prove that current, previous, last-applied, and container-referenced image
IDs never reach ``docker image rm`` while an old unprotected image does.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


def _find_usable_bash() -> str | None:
    candidate = shutil.which("bash")
    if candidate is None:
        return None
    try:
        result = subprocess.run(
            [candidate, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if result.returncode == 0 else None


_BASH = _find_usable_bash()
_CYGPATH = shutil.which("cygpath")
_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "box" / "onebrain-host-maintenance.sh"


def _unix(path: Path | str) -> str:
    if _CYGPATH is None:
        return str(path)
    return subprocess.run([_CYGPATH, "-u", str(path)], capture_output=True, text=True).stdout.strip()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


_DOCKER_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  ps)
    cat "$CTRL/containers"
    ;;
  inspect)
    target="${@: -1}"
    awk -v target="$target" '$1 == target { print $2; found=1; exit } END { exit found ? 0 : 1 }' "$CTRL/container-images"
    ;;
  image)
    case "$2" in
      inspect)
        target="${@: -1}"
        # The fixture uses digest-shaped local image IDs and simple non-digest
        # release refs, so it stays independent of Docker's Go-template quoting.
        if [[ "$target" == sha256:* ]]; then
          awk -v target="$target" '$1 == target { print $2; found=1; exit } END { exit found ? 0 : 1 }' "$CTRL/images"
        else
          awk -v target="$target" '$1 == target { print $2; found=1; exit } END { exit found ? 0 : 1 }' "$CTRL/ref-ids"
        fi
        ;;
      ls)
        awk '{ print $1 }' "$CTRL/images"
        ;;
      rm)
        echo "$3" >> "$CTRL/removed"
        ;;
      *) exit 64 ;;
    esac
    ;;
  builder)
    echo "builder-prune" >> "$CTRL/builder"
    ;;
  *) exit 64 ;;
esac
'''


_PYTHON_STUB = r'''#!/usr/bin/env bash
exec "$PYREAL" "$@"
'''

_VOLUME_VERIFY_STUB = r'''#!/usr/bin/env bash
[ "${1:-}" = "verify" ] || exit 64
[ -f "$CTRL/data_volume_fail" ] && exit 1
exit 0
'''

_MOUNTPOINT_STUB = r'''#!/usr/bin/env bash
[ "${1:-}" = "-q" ] || exit 64
[ -f "$CTRL/mountpoint_fail" ] && exit 1
exit 0
'''

_READLINK_STUB = r'''#!/usr/bin/env bash
path=""
for arg in "$@"; do path="$arg"; done
printf '%s\n' "$path"
'''

_FINDMNT_STUB = r'''#!/usr/bin/env bash
kind=""
target=""
for arg in "$@"; do
  case "$arg" in SOURCE|TARGET) kind="$arg" ;; esac
  target="$arg"
done
case "$kind" in
  SOURCE)
    if [ "$target" = "$ONEBRAIN_MAINTENANCE_DIR" ] && [ -f "$CTRL/maintenance_source_mismatch" ]; then
      printf 'nested-source\n'
    else
      printf 'verified-source\n'
    fi
    ;;
  TARGET)
    if [ -f "$CTRL/maintenance_target_mismatch" ]; then
      printf '%s\n' "$ONEBRAIN_MAINTENANCE_DIR"
    else
      printf '%s\n' "$ONEBRAIN_DATA_MOUNT"
    fi
    ;;
  *) exit 64 ;;
esac
'''


class _MaintenanceHarness:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.ctrl = root / "ctrl"
        self.mount = root / "data-mount"
        self.data = self.mount / "onebrain-maintenance"
        for path in (self.bin, self.ctrl, self.data / "onebrain_update"):
            path.mkdir(parents=True, exist_ok=True)
        _write_executable(self.bin / "docker", _DOCKER_STUB)
        _write_executable(self.bin / "python3", _PYTHON_STUB)
        _write_executable(self.bin / "onebrain-data-volume.sh", _VOLUME_VERIFY_STUB)
        _write_executable(self.bin / "mountpoint", _MOUNTPOINT_STUB)
        _write_executable(self.bin / "readlink", _READLINK_STUB)
        _write_executable(self.bin / "findmnt", _FINDMNT_STUB)
        (self.ctrl / "containers").write_text("container-running\n", encoding="utf-8")
        (self.ctrl / "container-images").write_text(
            "container-running sha256:running\n", encoding="utf-8")
        (self.ctrl / "images").write_text(
            "\n".join((
                "sha256:base 2020-01-01T00:00:00Z",
                "sha256:current 2020-01-01T00:00:00Z",
                "sha256:previous 2020-01-01T00:00:00Z",
                "sha256:applied 2020-01-01T00:00:00Z",
                "sha256:running 2020-01-01T00:00:00Z",
                "sha256:stale 2020-01-01T00:00:00Z",
            )) + "\n",
            encoding="utf-8",
        )
        (self.ctrl / "ref-ids").write_text(
            "\n".join((
                "base-ref sha256:base",
                "current-ref sha256:current",
                "previous-ref sha256:previous",
                "applied-ref sha256:applied",
            )) + "\n",
            encoding="utf-8",
        )
        (root / "docker-compose.yml").write_text(
            "services:\n  onebrain-api:\n    image: base-ref\n", encoding="utf-8")
        (root / "images.override.yml").write_text(
            "services:\n  onebrain-api:\n    image: current-ref\n", encoding="utf-8")
        (root / "images.override.prev.yml").write_text(
            "services:\n  onebrain-api:\n    image: previous-ref\n", encoding="utf-8")
        (self.data / "onebrain_update" / "last_applied.json").write_text(
            json.dumps({"images": {"onebrain-api": "applied-ref"}}), encoding="utf-8")

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        assert _BASH is not None
        env = {
            **os.environ,
            "PATH": f"{_unix(self.bin)}:{os.environ.get('PATH', '')}",
            "UPDATE_COMPOSE_DIR": _unix(self.root),
            "ONEBRAIN_DATA_MOUNT": _unix(self.mount),
            "ONEBRAIN_MAINTENANCE_DIR": _unix(self.data),
            "ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT": _unix(self.bin / "onebrain-data-volume.sh"),
            "MAINTENANCE_IMAGE_MIN_AGE_HOURS": "0",
            "MAINTENANCE_BUILD_CACHE_MIN_AGE_HOURS": "0",
            "STUB_LOG": _unix(self.ctrl / "docker.log"),
            "CTRL": _unix(self.ctrl),
            "PYREAL": _unix(Path(os.sys.executable)),
        }
        return subprocess.run(
            [_BASH, _unix(_SCRIPT), *args],
            env={**env, **extra_env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def removed(self) -> list[str]:
        path = self.ctrl / "removed"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def builder_ran(self) -> bool:
        return (self.ctrl / "builder").exists()


def test_host_maintenance_script_has_no_crlf_or_blind_image_prune():
    source = _SCRIPT.read_text(encoding="utf-8")
    assert b"\r" not in _SCRIPT.read_bytes()
    assert "images.override.yml" in source
    assert "images.override.prev.yml" in source
    assert "last_applied.json" in source
    assert "docker image prune" not in source
    assert '"$DOCKER" image rm "$image_id"' in source
    assert '"$DOCKER" builder prune --all --force' in source


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_host_maintenance_keeps_protected_images_and_removes_only_stale(tmp_path: Path):
    harness = _MaintenanceHarness(tmp_path)

    result = harness.run()

    assert result.returncode == 0, result.stderr
    assert harness.removed() == ["sha256:stale"]
    assert harness.builder_ran()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_host_maintenance_fails_closed_when_an_override_cannot_be_parsed(tmp_path: Path):
    harness = _MaintenanceHarness(tmp_path)
    (tmp_path / "images.override.yml").write_text("services: {}\n", encoding="utf-8")

    result = harness.run()

    assert result.returncode == 0, result.stderr
    assert harness.removed() == []


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_host_maintenance_holds_without_the_verified_data_volume(tmp_path: Path):
    harness = _MaintenanceHarness(tmp_path)
    (harness.ctrl / "data_volume_fail").write_text("", encoding="utf-8")

    result = harness.run()

    assert result.returncode == 0, result.stderr
    assert harness.removed() == []
    assert not harness.builder_ran()
    assert "persistent data volume is unavailable or mismatched" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_host_maintenance_holds_when_maintenance_subtree_is_a_different_mount(tmp_path: Path):
    harness = _MaintenanceHarness(tmp_path)
    (harness.ctrl / "maintenance_source_mismatch").write_text("", encoding="utf-8")

    result = harness.run()

    assert result.returncode == 0, result.stderr
    assert harness.removed() == []
    assert not harness.builder_ran()
    assert "maintenance directory is not on the verified data volume" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_host_maintenance_reclaims_a_stale_dead_update_lock(tmp_path: Path):
    harness = _MaintenanceHarness(tmp_path)
    lock = harness.data / "onebrain_update" / "update.lock"
    lock.mkdir()
    (lock / "pid").write_text("not-a-pid\n", encoding="utf-8")
    (lock / "started_at").write_text("0\n", encoding="utf-8")

    result = harness.run(UPDATE_LOCK_STALE_SECONDS="0")

    assert result.returncode == 0, result.stderr
    assert harness.removed() == ["sha256:stale"]
    assert not lock.exists()

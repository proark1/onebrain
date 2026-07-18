"""Focused contracts and fake-Docker functional tests for collation repair."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


def _find_usable_bash() -> str | None:
    candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git is not None:
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for candidate in dict.fromkeys(candidates):
        if candidate is None:
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


_BASH = _find_usable_bash()
_CYGPATH = shutil.which("cygpath")
_BOX_DIR = Path(__file__).resolve().parents[1] / "deploy" / "box"
_SCRIPT = _BOX_DIR / "onebrain-postgres-collation.sh"


def _unix(path: Path | str) -> str:
    if _CYGPATH is None:
        # Git Bash uses /c/... paths.  Backslashes are shell escapes when
        # box.env is sourced, while C:/... would be split at ':' in PATH.
        raw = str(path).replace("\\", "/")
        if len(raw) >= 3 and raw[1:3] == ":/":
            return f"/{raw[0].lower()}/{raw[3:]}"
        return raw
    return subprocess.run([_CYGPATH, "-u", str(path)], capture_output=True, text=True).stdout.strip()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


_DOCKER_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "docker $*" >> "$STUB_LOG"
[ "$1" = "compose" ] || exit 64
args=" $* "
if [[ "$args" == *" config -q "* ]]; then
  [ -f "$CTRL/config_fail" ] && exit 1
  exit 0
fi
if [[ "$args" == *" config --services "* ]]; then
  [ -f "$CTRL/discovery_fail" ] && exit 1
  cat "$CTRL/services"
  exit 0
fi
if [[ "$args" == *" stop "* ]]; then
  [ -f "$CTRL/stop_fail" ] && exit 1
  exit 0
fi
if [[ "$args" == *" up "* ]]; then
  exit 0
fi
if [[ "$args" == *" exec "* ]]; then
  if [[ "$args" == *" pg_dump "* ]]; then
    printf 'fake custom-format archive\n'
    exit 0
  fi
  sql="${@: -1}"
  if [[ "$sql" == *"datcollversion"* ]]; then
    [ -f "$CTRL/resolved" ] || cat "$CTRL/mismatched"
    exit 0
  fi
  if [[ "$sql" == *"pg_collation"* ]]; then
    printf '0\n'
    exit 0
  fi
  if [[ "$sql" == *"pg_database_size"* ]]; then
    cat "$CTRL/database-bytes"
    exit 0
  fi
  if [[ "$sql" == *"REINDEX"* ]]; then
    printf 'reindex\n' >> "$CTRL/actions"
    exit 0
  fi
  if [[ "$sql" == *"REFRESH COLLATION VERSION"* ]]; then
    touch "$CTRL/resolved"
    printf 'refresh\n' >> "$CTRL/actions"
    exit 0
  fi
fi
exit 0
'''


_OPENSSL_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "openssl $*" >> "$STUB_LOG"
out=""
previous=""
for arg in "$@"; do
  if [ "$previous" = "-out" ]; then out="$arg"; fi
  previous="$arg"
done
[ -n "$out" ] || exit 64
cat > "$out"
'''


_CURL_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "curl $*" >> "$STUB_LOG"
exit 0
'''


_DF_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
free="$(cat "$CTRL/free-bytes")"
printf '%s\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'
printf 'stub 100000000 0 %s 0%% /data\n' "$((free / 1024))"
'''


_MOUNTPOINT_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "mountpoint $*" >> "$STUB_LOG"
[ -f "$CTRL/mount_fail" ] && exit 1
[ "${@: -1}" = "$ONEBRAIN_DATA_MOUNT" ]
'''


_FINDMNT_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "findmnt $*" >> "$STUB_LOG"
[ "${@: -1}" = "$ONEBRAIN_MAINTENANCE_DIR" ] || exit 1
[ -f "$CTRL/nested_mount" ] && { printf '%s\n' "$ONEBRAIN_MAINTENANCE_DIR"; exit 0; }
printf '%s\n' "$ONEBRAIN_DATA_MOUNT"
'''


_DATA_VOLUME_VERIFY_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "data-volume-verify $*" >> "$STUB_LOG"
[ -f "$CTRL/volume_verify_fail" ] && exit 1
exit 0
'''


_FLOCK_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "flock $*" >> "$STUB_LOG"
if [ "$1" = "-n" ] && [ -f "$CTRL/lock_busy" ]; then exit 1; fi
exit 0
'''


_INSTALL_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
echo "install $*" >> "$STUB_LOG"
target="${@: -1}"
mkdir -p "$target"
chmod 0700 "$target"
'''


class _CollationHarness:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.ctrl = root / "ctrl"
        # The desktop sandbox permits Git Bash writes under /tmp but not a
        # mkdir -p traversal through the workspace's /c/... mount.  Python's
        # default temporary directory is that same location on this host.
        self.data = Path(tempfile.mkdtemp(prefix="onebrain-collation-"))
        self.data_posix = f"/tmp/{self.data.name}"
        self.maintenance_dir_posix = f"{self.data_posix}/maintenance"
        for path in (self.bin, self.ctrl):
            path.mkdir(parents=True, exist_ok=True)
        _write_executable(self.bin / "docker", _DOCKER_STUB)
        _write_executable(self.bin / "openssl", _OPENSSL_STUB)
        _write_executable(self.bin / "curl", _CURL_STUB)
        _write_executable(self.bin / "df", _DF_STUB)
        _write_executable(self.bin / "mountpoint", _MOUNTPOINT_STUB)
        _write_executable(self.bin / "findmnt", _FINDMNT_STUB)
        _write_executable(self.bin / "data-volume-verify", _DATA_VOLUME_VERIFY_STUB)
        _write_executable(self.bin / "flock", _FLOCK_STUB)
        _write_executable(self.bin / "install", _INSTALL_STUB)
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        # The real compose CLI emits LF records.  Use bytes here because a
        # Windows text-mode test fixture would otherwise manufacture CRs that
        # the host script correctly rejects as unsafe service names.
        (self.ctrl / "services").write_bytes(
            b"postgres\npostgres-roles\nredis\ncaddy\nonebrain-migrate\nonebrain-api\nonebrain-workers\n"
        )
        (self.ctrl / "mismatched").write_bytes(b"onebrain\n")
        (self.ctrl / "database-bytes").write_bytes(b"1048576\n")
        self.set_free_bytes(1024 * 1024 * 1024)
        self._write_box_env("K" * 32)

    @property
    def maintenance_dir(self) -> Path:
        return self.data / "maintenance"

    @property
    def backup_dir(self) -> Path:
        return self.maintenance_dir / "collation-backups"

    def _write_box_env(self, backup_key: str, maintenance_dir: str | None = None) -> None:
        self.backup_key = backup_key
        if maintenance_dir is not None:
            self.maintenance_dir_posix = maintenance_dir
        values = {
            # Deliberately point this legacy container path away from the
            # verified host volume; the collation script must never use it.
            "UPDATE_DATA_DIR": "/root-disk-must-not-be-used",
            "ONEBRAIN_DATA_MOUNT": self.data_posix,
            "ONEBRAIN_MAINTENANCE_DIR": self.maintenance_dir_posix,
            "ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT": _unix(self.bin / "data-volume-verify"),
            "UPDATE_COMPOSE_DIR": _unix(self.root),
            "UPDATE_COMPOSE_PROJECT": "onebrain-test",
            "UPDATE_PROFILES": "onebrain",
            "UPDATE_HEALTH_URL": "http://127.0.0.1/health",
            "UPDATE_BACKUP_KEY": backup_key,
            "ONEBRAIN_COLLATION_BACKUP_RETENTION_DAYS": "30",
        }
        (self.root / "box.env").write_bytes(
            ("\n".join(f"{key}={value}" for key, value in values.items()) + "\n").encode("utf-8"),
        )

    def set_backup_key(self, backup_key: str) -> None:
        self._write_box_env(backup_key)

    def set_maintenance_dir(self, maintenance_dir: str) -> None:
        self._write_box_env(self.backup_key, maintenance_dir)

    def set_free_bytes(self, value: int) -> None:
        (self.ctrl / "free-bytes").write_bytes(f"{value}\n".encode("utf-8"))

    def touch(self, name: str) -> None:
        (self.ctrl / name).touch()

    def run(self, mode: str) -> subprocess.CompletedProcess[str]:
        assert _BASH is not None
        env = {
            **os.environ,
            "BOX_ENV": _unix(self.root / "box.env"),
            "ENV_FILE": _unix(self.root / ".env"),
            "CTRL": _unix(self.ctrl),
            "STUB_LOG": _unix(self.ctrl / "stub.log"),
        }
        command = f'export PATH="{_unix(self.bin)}:$PATH"; exec "{_unix(_SCRIPT)}" {mode}'
        return subprocess.run(
            [_BASH, "-c", command],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def log(self) -> str:
        path = self.ctrl / "stub.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def actions(self) -> list[str]:
        path = self.ctrl / "actions"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def cleanup(self) -> None:
        shutil.rmtree(self.data, ignore_errors=True)


@pytest.fixture()
def collation(tmp_path: Path):
    harness = _CollationHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.cleanup()


def test_collation_script_has_safe_check_and_apply_contracts():
    source = _SCRIPT.read_text(encoding="utf-8")

    assert b"\r" not in _SCRIPT.read_bytes()
    assert 'MODE="${1:-check}"' in source
    assert "  check|apply) ;;" in source
    assert 'if [ "$MODE" = "check" ]; then' in source
    assert "check complete; rerun with apply during a maintenance window" in source
    assert "datname <> 'template0'" in source
    assert 'dc_over "${PROFILE_ARGS[@]}" config -q' in source
    assert 'config --services 2>/dev/null' in source
    assert 'if [ "$MODE" = "apply" ]; then\n  validate_backup_key\n  verify_maintenance_directory\n  acquire_maintenance_lock' in source
    assert 'ONEBRAIN_DATA_MOUNT:=${ONEBRAIN_DATA_VOLUME_PATH:-/mnt/onebrain-data}' in source
    assert 'ONEBRAIN_MAINTENANCE_DIR:=${ONEBRAIN_DATA_MOUNT}/maintenance' in source
    assert 'mountpoint -q "$ONEBRAIN_DATA_MOUNT"' in source
    assert 'findmnt -nr -o TARGET --target "$maintenance_real"' in source
    assert 'UPDATE_DATA_DIR' not in source


def test_collation_script_encrypts_and_sizes_backups_conservatively():
    source = _SCRIPT.read_text(encoding="utf-8")

    assert 'backup_key="${UPDATE_BACKUP_KEY:-}"' in source
    assert '"${#backup_key}" -lt 32' in source
    assert "pg_database_size(datname)" in source
    assert "ONEBRAIN_COLLATION_BACKUP_MARGIN_PERCENT:=25" in source
    assert "ONEBRAIN_COLLATION_BACKUP_MIN_MARGIN_BYTES:=268435456" in source
    assert "-pass env:UPDATE_BACKUP_KEY" in source
    assert "pass:${UPDATE_BACKUP_KEY" not in source
    assert 'umask 077' in source
    assert 'chmod 0700 "$BACKUP_DIR"' in source
    assert 'chmod 0600 "$backup"' in source
    assert "retain_encrypted_backups()" in source
    assert "[ \"$backup\" = \"$newest\" ] && continue" in source
    assert 'mktemp "$BACKUP_DIR/${database}-${timestamp}-XXXXXXXX.dump.enc"' in source
    assert '"$FLOCK" -n "$MAINTENANCE_LOCK_FD"' in source


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_check_mode_is_read_only_and_does_not_require_apply_secrets(collation: _CollationHarness):
    harness = collation
    harness.set_backup_key("short")
    harness.touch("config_fail")

    result = harness.run("check")

    assert result.returncode == 0, result.stderr
    assert "config" not in harness.log()
    assert "pg_dump" not in harness.log()
    assert "REINDEX" not in harness.log()
    assert "mountpoint" not in harness.log()
    assert "flock" not in harness.log()
    assert not harness.maintenance_dir.exists()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
@pytest.mark.parametrize("failure_marker", ["config_fail", "discovery_fail"])
def test_apply_holds_before_backup_or_reindex_when_config_or_service_discovery_fails(
    collation: _CollationHarness, failure_marker: str
):
    harness = collation
    harness.touch(failure_marker)

    result = harness.run("apply")

    assert result.returncode != 0
    assert "pg_dump" not in harness.log()
    assert " stop " not in harness.log()
    assert harness.actions() == []
    assert not harness.backup_dir.exists()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
@pytest.mark.parametrize("failure_marker", ["mount_fail", "volume_verify_fail"])
def test_apply_holds_before_query_when_data_volume_is_not_verified(
    collation: _CollationHarness, failure_marker: str
):
    harness = collation
    harness.touch(failure_marker)

    result = harness.run("apply")

    assert result.returncode != 0
    assert "docker " not in harness.log()
    assert "flock" not in harness.log()
    assert not harness.maintenance_dir.exists()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_rejects_maintenance_directory_outside_verified_volume(collation: _CollationHarness):
    harness = collation
    harness.set_maintenance_dir("/tmp/not-onebrain-maintenance")

    result = harness.run("apply")

    assert result.returncode != 0
    assert "docker " not in harness.log()
    assert "flock" not in harness.log()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_rejects_a_maintenance_directory_that_resolves_to_nested_mount(collation: _CollationHarness):
    harness = collation
    harness.touch("nested_mount")

    result = harness.run("apply")

    assert result.returncode != 0
    assert "docker " not in harness.log()
    assert "flock" not in harness.log()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_recovers_known_services_after_partial_stop_failure(collation: _CollationHarness):
    harness = collation
    harness.touch("stop_fail")

    result = harness.run("apply")

    assert result.returncode != 0
    log = harness.log()
    stop = log.index(" stop onebrain-api onebrain-workers")
    resume = log.index(" up -d onebrain-api onebrain-workers")
    assert stop < resume
    assert harness.actions() == []


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_holds_before_query_when_another_host_maintenance_run_owns_lock(collation: _CollationHarness):
    harness = collation
    harness.touch("lock_busy")

    result = harness.run("apply")

    assert result.returncode != 0
    assert "flock -n" in harness.log()
    assert "docker " not in harness.log()
    assert harness.actions() == []


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_validates_backup_key_before_mutating(collation: _CollationHarness):
    harness = collation
    secret = "too-short-key"
    harness.set_backup_key(secret)

    result = harness.run("apply")

    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr + harness.log()
    assert "config" not in harness.log()
    assert "pg_dump" not in harness.log()
    assert harness.actions() == []
    assert "mountpoint" not in harness.log()
    assert "flock" not in harness.log()


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_uses_live_database_size_for_capacity_gate(collation: _CollationHarness):
    harness = collation
    # 1 MiB of database data needs the 256 MiB safety floor in addition to the
    # actual size, not the former fixed 1 GiB threshold.
    required = 1_048_576 + 268_435_456
    harness.set_free_bytes(required - 1024)

    result = harness.run("apply")

    assert result.returncode != 0
    assert f"need {required} bytes" in result.stderr
    assert "pg_dump" not in harness.log()
    assert harness.actions() == []


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable")
def test_apply_encrypts_backups_and_safely_retains_only_expired_archives(collation: _CollationHarness):
    harness = collation
    backup_dir = harness.backup_dir
    backup_dir.mkdir(parents=True)
    expired = backup_dir / "onebrain-20200101T000000Z.dump.enc"
    unrelated = backup_dir / "not-a-collation-backup.dump.enc"
    expired.write_bytes(b"old encrypted archive")
    unrelated.write_bytes(b"leave this file alone")
    old = time.time() - 40 * 24 * 60 * 60
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))

    result = harness.run("apply")

    assert result.returncode == 0, result.stderr
    assert harness.actions() == ["reindex", "refresh"]
    assert not expired.exists()
    assert unrelated.exists()
    backups = sorted(backup_dir.glob("onebrain-*.dump.enc"))
    assert len(backups) == 1
    assert re.fullmatch(r"onebrain-\d{8}T\d{6}Z-[A-Za-z0-9]{8}\.dump\.enc", backups[0].name)
    assert backups[0].read_bytes() == b"fake custom-format archive\n"
    assert not list(backup_dir.glob("*.dump"))
    log = harness.log()
    assert "openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:UPDATE_BACKUP_KEY" in log
    assert "K" * 32 not in result.stdout + result.stderr + log
    assert log.index("flock -n") < log.index("docker compose")

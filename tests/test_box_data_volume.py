"""Safety contracts for the persistent OneBrain data-volume setup script."""

from __future__ import annotations

import os
import shutil
import subprocess
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
_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "box" / "onebrain-data-volume.sh"


def _matches(entry: str, uuid: str = "4fe1d2c3-b4a5-6789-0123-456789abcdef") -> bool:
    assert _BASH is not None
    env = os.environ.copy()
    env.update({
        "SCRIPT_PATH": str(_SCRIPT),
        "ENTRY": entry,
        "EXPECTED_UUID": uuid,
        "ONEBRAIN_DATA_MOUNT": "/mnt/onebrain-data",
    })
    result = subprocess.run(
        [_BASH, "-c", 'source "$SCRIPT_PATH"; fstab_entry_matches_volume "$ENTRY" "$EXPECTED_UUID"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


@pytest.mark.skipif(_BASH is None, reason="functional bash unavailable for fstab contract")
def test_existing_safe_fstab_options_are_accepted_but_identity_drift_is_rejected():
    uuid = "4fe1d2c3-b4a5-6789-0123-456789abcdef"
    assert _matches(f"UUID={uuid} /mnt/onebrain-data ext4 defaults,nofail 0 2", uuid)
    assert _matches(f"UUID={uuid} /mnt/onebrain-data ext4 defaults,x-systemd.device-timeout=90s 0 2", uuid)

    assert not _matches(f"UUID=wrong /mnt/onebrain-data ext4 defaults,nofail 0 2", uuid)
    assert not _matches(f"UUID={uuid} /other-mount ext4 defaults,nofail 0 2", uuid)
    assert not _matches(f"UUID={uuid} /mnt/onebrain-data xfs defaults,nofail 0 2", uuid)


def test_setup_rejects_duplicate_mount_entries_instead_of_guessing():
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "multiple fstab entries already own $DATA_MOUNT" in source
    assert 'fstab_entry_matches_volume "${current_entries[0]}" "$uuid"' in source

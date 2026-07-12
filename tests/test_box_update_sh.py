"""P4-08: the update.sh dry-run harness. Stubs docker/curl/alembic/pg_dump/
pg_restore/openssl on a temp PATH and runs the REAL deploy/box/update.sh under
bash with the REAL app-free verifier. No Docker/Postgres/network. Skips entirely
if bash is absent (A4: shellcheck is CI-only and never a local gate).

A1: asserts update.sh contains no CR before executing (a CRLF checkout would break
Cygwin/Git-Bash on `set -euo pipefail\\r`)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.trust.envelope import DesiredStateEnvelope, SignedReleaseBlock, sign_desired_state
from app.trust.release import canonical_release_payload
from app.trust.signing import generate_keypair, sign_payload

_BASH = shutil.which("bash")
_CYGPATH = shutil.which("cygpath")
_BOX_DIR = Path(__file__).resolve().parents[1] / "deploy" / "box"
_UPDATE_SH = _BOX_DIR / "update.sh"
_VERIFY_PY = _BOX_DIR / "onebrain_box_verify.py"

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash unavailable (harness needs /usr/bin/bash)")

REL_PRIV, REL_PUB = generate_keypair()
DS_PRIV, DS_PUB = generate_keypair()
GOOD_IMG = "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64

_STUBS = {
    "docker": (
        '#!/usr/bin/env bash\n'
        'echo "docker $*" >> "$STUB_LOG"\n'
        'if [ "$1" = "pull" ]; then echo "$2" >> "$CTRL/pulled_refs"; fi\n'
        'exit 0\n'
    ),
    "curl": (
        '#!/usr/bin/env bash\n'
        'echo "curl $*" >> "$STUB_LOG"\n'
        'url=""; for a in "$@"; do url="$a"; done\n'
        'case "$url" in\n'
        '  *desired-state*) [ -f "$CTRL/fetch_fail" ] && exit 22; cat "$CTRL/serve.json" ;;\n'
        '  *health*) [ -f "$CTRL/smoke_fail" ] && exit 22; printf "OK" ;;\n'
        '  *) : ;;\n'
        'esac\n'
        'exit 0\n'
    ),
    "alembic": (
        '#!/usr/bin/env bash\n'
        'echo "alembic $*" >> "$STUB_LOG"\n'
        'if [ "$1" = "current" ]; then cat "$CTRL/alembic_current" 2>/dev/null || echo ""; fi\n'
        'exit 0\n'
    ),
    "pg_dump": (
        '#!/usr/bin/env bash\n'
        'echo "pg_dump $*" >> "$STUB_LOG"\n'
        'printf -- "-- fake dump\\n"\n'
        'exit 0\n'
    ),
    "pg_restore": (
        '#!/usr/bin/env bash\n'
        'echo "pg_restore $*" >> "$STUB_LOG"\n'
        # The input dump is the last arg; record whether it actually exists so the
        # dry-run test can prove the recover path decrypted a real backup FIRST (the
        # plaintext dump was shredded post-encrypt, so a naive restore would find no file).
        'in=""; for a in "$@"; do in="$a"; done\n'
        'if [ -n "$in" ] && [ -f "$in" ]; then echo "pg_restore INPUT_PRESENT $in" >> "$STUB_LOG"; '
        'else echo "pg_restore INPUT_MISSING $in" >> "$STUB_LOG"; fi\n'
        'exit 0\n'
    ),
    "openssl": (
        '#!/usr/bin/env bash\n'
        'echo "openssl $*" >> "$STUB_LOG"\n'
        'prev=""; for a in "$@"; do if [ "$prev" = "-out" ]; then : > "$a"; fi; prev="$a"; done\n'
        'exit 0\n'
    ),
    "python3": (
        '#!/usr/bin/env bash\n'
        'exec "$PYREAL" "$@"\n'
    ),
}


def _unix(path) -> str:
    # On Linux/macOS the path is already POSIX — cygpath is a Git-Bash/Cygwin-only
    # tool (absent on CI), so converting is both unnecessary and a None-deref.
    if _CYGPATH is None:
        return str(path)
    return subprocess.run([_CYGPATH, "-u", str(path)], capture_output=True, text=True).stdout.strip()


def signed_serve(*, version="2026.7.2", images=None, migration_from="0020", migration_to="0020",
                 rollback_kind="code_only", tamper_wrapper=False, attempt_id="child_1") -> dict:
    images = images if images is not None else {"onebrain-api": GOOD_IMG}
    fields = dict(version=version, git_sha="abc", modules={"onebrain-api": version}, images=images,
                  migration_from=migration_from, migration_to=migration_to, rollback_kind=rollback_kind)
    relsig = sign_payload(canonical_release_payload(**fields), REL_PRIV)
    block = SignedReleaseBlock(signature=relsig, **fields)
    env = DesiredStateEnvelope(deployment_id="dep_a", release=block, version_floor="",
                               nonce="nonce-" + version.replace(".", ""),
                               issued_at="2026-07-12T00:00:00+00:00", expires_at="2035-01-01T00:00:00+00:00")
    env = sign_desired_state(env, DS_PRIV)
    dumped = env.model_dump()
    if tamper_wrapper:
        dumped["envelope_signature"] = base64.b64encode(b"z" * 64).decode()
    return {"envelope": dumped, "attempt_id": attempt_id}


class _Harness:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.ctrl = root / "ctrl"
        self.data = root / "data"
        for d in (self.bin, self.ctrl, self.data):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in _STUBS.items():
            p = self.bin / name
            p.write_bytes(body.encode("utf-8"))
            os.chmod(p, 0o755)
        # placeholder compose whose baked digest DIFFERS from the verified one (A7).
        (root / "docker-compose.yml").write_bytes(
            ("services:\n  onebrain-api:\n    image: ghcr.io/proark1/onebrain-api@sha256:"
             + "f" * 64 + "\n").encode("utf-8"))
        self._write_box_env()

    def _write_box_env(self):
        lines = {
            "ONEBRAIN_FLEET_URL": "https://mc.test",
            "ONEBRAIN_FLEET_KEY": "fk_test",
            "ONEBRAIN_DEPLOYMENT_ID": "dep_a",
            "UPDATE_DESIRED_STATE_PUBLIC_KEY": DS_PUB,
            "UPDATE_RELEASE_PUBLIC_KEY": REL_PUB,
            "UPDATE_REGISTRY_ALLOWLIST": "ghcr.io/proark1",
            "UPDATE_DATA_DIR": _unix(self.data),
            "UPDATE_COMPOSE_DIR": _unix(self.root),
            "UPDATE_COMPOSE_PROJECT": "onebrain-dep_a",
            "UPDATE_PROFILES": "onebrain",
            "UPDATE_LOCAL_MODULES": "onebrain-api",
            "UPDATE_HEALTH_URL": "http://127.0.0.1/health",
            "UPDATE_VERIFY_BIN": _unix(_VERIFY_PY),
            "UPDATE_BACKUP_KEY": "testbackupkey",
        }
        (self.root / "box.env").write_bytes(
            ("\n".join(f"{k}={v}" for k, v in lines.items()) + "\n").encode("utf-8"))

    def set_serve(self, serve: dict):
        (self.ctrl / "serve.json").write_bytes(json.dumps(serve).encode("utf-8"))

    def touch(self, name: str):
        (self.ctrl / name).write_bytes(b"")

    def set_alembic_current(self, rev: str):
        (self.ctrl / "alembic_current").write_bytes(rev.encode("utf-8"))

    def seed_last_applied(self, images: dict):
        work = self.data / "onebrain_update"
        work.mkdir(parents=True, exist_ok=True)
        (work / "last_applied.json").write_bytes(json.dumps({"images": images}).encode("utf-8"))

    def run(self) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "BOX_ENV": _unix(self.root / "box.env"),
            "STUB_LOG": _unix(self.ctrl / "stub.log"),
            "CTRL": _unix(self.ctrl),
            "PYREAL": _unix(os.sys.executable),
        }
        cmd = f'export PATH="{_unix(self.bin)}:$PATH"; exec "{_unix(_UPDATE_SH)}"'
        return subprocess.run([_BASH, "-c", cmd], env=env, capture_output=True, text=True, timeout=120)

    def state(self):
        p = self.data / "onebrain_update" / "update_state.json"
        return json.loads(p.read_text()) if p.exists() else None

    def pulled(self):
        p = self.ctrl / "pulled_refs"
        return [line for line in p.read_text().splitlines() if line.strip()] if p.exists() else []

    def stub_log(self):
        p = self.ctrl / "stub.log"
        return p.read_text() if p.exists() else ""


@pytest.fixture()
def box(tmp_path):
    return _Harness(tmp_path)


# --- A1 ----------------------------------------------------------------------
def test_update_sh_has_no_crlf():
    assert b"\r" not in _UPDATE_SH.read_bytes()
    assert b"\r" not in _VERIFY_PY.read_bytes()


# --- happy paths -------------------------------------------------------------
def test_happy_path_no_migration(box):
    box.set_serve(signed_serve(migration_from="0020", migration_to="0020"))
    result = box.run()
    assert result.returncode == 0, result.stderr
    state = box.state()
    assert state is not None and state["outcome"] == "succeeded"
    assert state["attempt_id"] == "child_1"
    assert box.pulled() == [GOOD_IMG]
    assert "pg_dump" not in box.stub_log()   # no schema change -> no backup


def test_pulled_digests_equal_verifier_output(box):
    # A7: the baked compose pins a DIFFERENT digest (f*64); the pull must still be
    # the VERIFIED digest (a*64), proving the pull is driven by verifier stdout.
    box.set_serve(signed_serve())
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.pulled() == [GOOD_IMG]
    assert ("f" * 64) not in "\n".join(box.pulled())


def test_verify_failure_holds(box):
    box.set_serve(signed_serve(tamper_wrapper=True))
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.pulled() == []                       # NO pull on a rejected envelope
    assert box.state()["outcome"] == "failed"
    assert "envelope_signature_invalid" in box.stub_log() or \
        "envelope_signature_invalid" in (box.data / "onebrain_update" / "update.log").read_text()


def test_migration_crossing_fences(box):
    box.set_serve(signed_serve(migration_from="0019", migration_to="0020", rollback_kind="restore_required"))
    box.set_alembic_current("0020")                 # migrate reached the target -> fence passes
    result = box.run()
    assert result.returncode == 0, result.stderr
    state = box.state()
    assert state["outcome"] == "succeeded"
    assert state["migration_reached"] == "0020"
    assert state["backup_status"] == "success"      # schema change -> backup taken
    assert "pg_dump" in box.stub_log() and "openssl" in box.stub_log()


def test_migration_crossing_fence_mismatch_holds_degraded(box):
    box.set_serve(signed_serve(migration_from="0019", migration_to="0020"))
    box.set_alembic_current("0019")                 # migrate did NOT reach target -> fence fails
    result = box.run()
    assert result.returncode == 0, result.stderr
    state = box.state()
    assert state["outcome"] == "failed"
    assert state["migration_reached"] == "0019"     # held degraded, no tag flap


# --- smoke-fail recovery -----------------------------------------------------
def test_smoke_fail_code_only_rolls_back(box):
    box.set_serve(signed_serve(rollback_kind="code_only", migration_from="0020", migration_to="0020"))
    box.touch("smoke_fail")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "rolled_back"
    assert "pg_restore" not in box.stub_log()       # code_only never restores the DB


def test_smoke_fail_restore_required_restores(box):
    box.set_serve(signed_serve(rollback_kind="restore_required", migration_from="0019", migration_to="0020"))
    box.set_alembic_current("0020")
    box.touch("smoke_fail")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "rolled_back"
    log = box.stub_log()
    assert "pg_restore" in log                       # restore_required -> pg_restore invoked
    # The recover path DECRYPTS the .enc backup (openssl enc -d) BEFORE pg_restore, and
    # pg_restore consumes an existing decrypted file — never the shredded plaintext dump.
    assert "enc -d" in log
    assert log.index("enc -d") < log.index("pg_restore")
    assert "pg_restore INPUT_PRESENT" in log
    assert "pg_restore INPUT_MISSING" not in log


def test_update_sh_recover_restore_decrypts_before_restore():
    """Static contract: the restore_required recover branch decrypts the encrypted
    backup (openssl enc -d) BEFORE pg_restore, and restores the DECRYPTED temp — never
    the shredded plaintext dump ($WORK/backup.sql)."""
    src = _UPDATE_SH.read_text(encoding="utf-8")
    start = src.index("recover_restore_required() {")
    body = src[start:src.index("\n}", start)]
    decrypt = body.index('"$OPENSSL" enc -d')                 # decrypts the encrypted backup
    restore = body.index('"$PG_RESTORE"')                     # restores from it
    assert decrypt < restore                                  # decrypt BEFORE restore
    assert '"$PG_RESTORE" "$WORK/backup.sql"' not in body     # never the deleted plaintext dump


# --- MC unreachable ----------------------------------------------------------
def test_mc_unreachable_holds_last_known_good(box):
    box.set_serve(signed_serve())
    box.touch("fetch_fail")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.pulled() == []                        # nothing destructive
    assert box.state() is None                       # no outcome written (a missed poll, not a rejection)


# --- shellcheck (CI-only; A4) ------------------------------------------------
def test_shellcheck_clean():
    checker = shutil.which("shellcheck")
    if checker is None:
        pytest.skip("shellcheck absent (CI-only gate; not a local merge gate — A4)")
    result = subprocess.run([checker, str(_UPDATE_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

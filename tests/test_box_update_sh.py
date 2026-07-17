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
_BOOTSTRAP_SH = _BOX_DIR / "onebrain_bootstrap.sh"
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
        '  *floor-bump*) [ -f "$CTRL/floor_bump_fail" ] && exit 22; '
        'cat "$CTRL/floor_bump_serve.json" 2>/dev/null || printf "null" ;;\n'
        '  *bootstrap*) [ -f "$CTRL/bootstrap_fail" ] && exit 22; '
        'cat "$CTRL/bootstrap_resp.json" 2>/dev/null || printf "" ;;\n'
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


def signed_floor_bump(floor_version="2026.7.5", *, scope="*", tamper=False) -> dict:
    """The MC serve shape {"floor_bump": <signed FloorBump.model_dump()>}. Signed with
    the OFFLINE release key (REL_PRIV) the box's UPDATE_RELEASE_PUBLIC_KEY verifies."""
    from app.trust.envelope import FloorBump, sign_floor_bump

    bump = sign_floor_bump(
        FloorBump(deployment_scope=scope, floor_version=floor_version,
                  issued_at="2026-07-12T00:00:00+00:00"),
        REL_PRIV,
    )
    dumped = bump.model_dump()
    if tamper:
        dumped["signature"] = base64.b64encode(b"z" * 64).decode()
    return {"floor_bump": dumped}


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
            "ONEBRAIN_BOOTSTRAP_TOKEN": "bt_harness_token",   # first-boot exchange auth (P5-03)
        }
        (self.root / "box.env").write_bytes(
            ("\n".join(f"{k}={v}" for k, v in lines.items()) + "\n").encode("utf-8"))

    def set_serve(self, serve: dict):
        (self.ctrl / "serve.json").write_bytes(json.dumps(serve).encode("utf-8"))

    def set_floor_bump_serve(self, served: dict):
        (self.ctrl / "floor_bump_serve.json").write_bytes(json.dumps(served).encode("utf-8"))

    def set_bootstrap_resp(self, resp: dict):
        (self.ctrl / "bootstrap_resp.json").write_bytes(json.dumps(resp).encode("utf-8"))

    def env_content(self):
        p = self.root / ".env"
        return p.read_text() if p.exists() else None

    def applied_epoch(self):
        p = self.data / "onebrain_update" / "secrets_epoch"
        return p.read_text().strip() if p.exists() else None

    def floor_state(self):
        p = self.data / "onebrain_update" / "floor_state.json"
        return json.loads(p.read_text()) if p.exists() else None

    def touch(self, name: str):
        (self.ctrl / name).write_bytes(b"")

    def set_alembic_current(self, rev: str):
        (self.ctrl / "alembic_current").write_bytes(rev.encode("utf-8"))

    def seed_last_applied(self, images: dict):
        work = self.data / "onebrain_update"
        work.mkdir(parents=True, exist_ok=True)
        (work / "last_applied.json").write_bytes(json.dumps({"images": images}).encode("utf-8"))

    def _env(self) -> dict:
        return {
            **os.environ,
            "BOX_ENV": _unix(self.root / "box.env"),
            # ENV_FILE isolates the scripts from any real /opt/onebrain/.env (P5-03). It
            # does not exist until onebrain_bootstrap.sh writes it, so update.sh's
            # .env-first source is a no-op on a fresh box.
            "ENV_FILE": _unix(self.root / ".env"),
            "STUB_LOG": _unix(self.ctrl / "stub.log"),
            "CTRL": _unix(self.ctrl),
            "PYREAL": _unix(os.sys.executable),
        }

    def _exec(self, script: Path) -> subprocess.CompletedProcess:
        cmd = f'export PATH="{_unix(self.bin)}:$PATH"; exec "{_unix(script)}"'
        return subprocess.run([_BASH, "-c", cmd], env=self._env(), capture_output=True, text=True, timeout=120)

    def run(self) -> subprocess.CompletedProcess:
        return self._exec(_UPDATE_SH)

    def run_bootstrap(self) -> subprocess.CompletedProcess:
        return self._exec(_BOOTSTRAP_SH)

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
    override = (box.root / "images.override.yml").read_text(encoding="utf-8")
    assert "onebrain-migrate:" in override
    assert override.count(GOOD_IMG) == 2


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
    restore = body.index('restore_onebrain_db "$RESTORE"')    # restores from it
    assert decrypt < restore                                  # decrypt BEFORE restore
    assert '"$WORK/backup.dump"' not in body                  # never the deleted plaintext dump


# --- MC unreachable ----------------------------------------------------------
def test_mc_unreachable_holds_last_known_good(box):
    box.set_serve(signed_serve())
    box.touch("fetch_fail")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.pulled() == []                        # nothing destructive
    assert box.state() is None                       # no outcome written (a missed poll, not a rejection)


# --- P5-01 floor-bump fetch + apply ------------------------------------------
def test_floor_bump_fetched_and_applied_raises_local_floor(box):
    # A served, signed bump is fetched in step 0 and applied by the box verifier;
    # the local floor rises. No desired-state served -> the run stops after step 0.
    box.set_floor_bump_serve(signed_floor_bump("2026.7.5"))
    result = box.run()
    assert result.returncode == 0, result.stderr
    floor = box.floor_state()
    assert floor is not None and floor["floor_version"] == "2026.7.5"


def test_floor_bump_step_precedes_desired_state_fetch():
    # Step 0 (kill-switch) must run BEFORE the desired-state fetch so a revoked box
    # raises its floor even if it would otherwise pull.
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert src.index("/api/fleet/floor-bump") < src.index("/api/fleet/desired-state")


def test_forged_floor_bump_is_rejected_box_side_and_held(box):
    # A bump with a broken signature is rejected by the box verifier (verify-don't-
    # trust): the floor does NOT rise, and the run continues non-fatally.
    box.set_floor_bump_serve(signed_floor_bump("2026.7.9", tamper=True))
    result = box.run()
    assert result.returncode == 0, result.stderr
    floor = box.floor_state()
    assert floor is None or floor.get("floor_version", "") != "2026.7.9"
    log = (box.data / "onebrain_update" / "update.log").read_text()
    assert "floor bump apply rejected" in log


def test_no_floor_bump_served_is_a_noop(box):
    # No served bump and no desired-state -> step 0 no-ops (serves "null"), the run
    # stops cleanly, and nothing is applied (no floor written, no outcome). The
    # happy-path tests above already cover step 0 not disturbing a real update.
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.floor_state() is None   # no bump applied
    assert box.state() is None         # no desired-state -> no outcome


# --- P5-03 bootstrap exchange (onebrain_bootstrap.sh) ------------------------
def test_bootstrap_first_boot_writes_env_then_records_epoch(box):
    # First boot (no .env) writes /opt/onebrain/.env from the served dotenv, and records
    # the applied epoch ONLY after the write (G1-2/G1-3).
    box.set_bootstrap_resp({"secrets_epoch": 0, "dotenv": "POSTGRES_PASSWORD=pg\nONEBRAIN_FLEET_KEY=fk_real\n"})
    result = box.run_bootstrap()
    assert result.returncode == 0, result.stderr
    env = box.env_content()
    assert env is not None and "POSTGRES_PASSWORD=pg" in env and "ONEBRAIN_FLEET_KEY=fk_real" in env
    assert box.applied_epoch() == "0"
    # First boot leaves `compose up` to the cloud-init runcmd (no dc up here).
    assert "up -d" not in box.stub_log()


def test_bootstrap_first_boot_accepts_unresolved_secret_placeholders(box):
    # The rendered first-boot box.env carries ${VAR} references until /bootstrap
    # returns the real secret bundle. Strict nounset must not abort while sourcing
    # those intentional placeholders.
    box_env = box.root / "box.env"
    content = box_env.read_text(encoding="utf-8").replace(
        "ONEBRAIN_FLEET_KEY=fk_test",
        "ONEBRAIN_FLEET_KEY=${ONEBRAIN_FLEET_KEY}",
    )
    box_env.write_text(content, encoding="utf-8")
    box.set_bootstrap_resp({
        "secrets_epoch": 0,
        "dotenv": "POSTGRES_PASSWORD=pg\nONEBRAIN_FLEET_KEY=fk_real\n",
    })

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    assert "ONEBRAIN_FLEET_KEY=fk_real" in (box.env_content() or "")
    assert box.applied_epoch() == "0"


def test_bootstrap_holds_on_unreachable_without_writing_env(box):
    # Non-2xx / unreachable -> HOLD: no .env, no epoch advance (non-destructive).
    box.touch("bootstrap_fail")
    result = box.run_bootstrap()
    assert result.returncode == 0, result.stderr
    assert box.env_content() is None
    assert box.applied_epoch() is None


def test_bootstrap_rotation_reapplies_only_on_higher_epoch(box):
    box.set_bootstrap_resp({"secrets_epoch": 0, "dotenv": "POSTGRES_PASSWORD=v0\n"})
    assert box.run_bootstrap().returncode == 0
    assert box.applied_epoch() == "0"

    # An equal/stale served epoch is a no-op: no rewrite, no compose up.
    box.set_bootstrap_resp({"secrets_epoch": 0, "dotenv": "POSTGRES_PASSWORD=SHOULD_NOT_APPLY\n"})
    assert box.run_bootstrap().returncode == 0
    assert "SHOULD_NOT_APPLY" not in (box.env_content() or "")

    # A higher served epoch re-writes .env AND re-applies (dc up -d) so containers reload.
    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "POSTGRES_PASSWORD=v1\n"})
    assert box.run_bootstrap().returncode == 0
    assert "POSTGRES_PASSWORD=v1" in box.env_content()
    assert box.applied_epoch() == "1"
    assert "compose" in box.stub_log() and "up -d" in box.stub_log()


def test_update_sh_sources_env_before_box_env():
    # Static: update.sh sources /opt/onebrain/.env (the exchanged secret bundle) BEFORE
    # box.env, so box.env's ${VAR} refs re-expand to the delivered real values (P5-03).
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert src.index('. "$ENV_FILE"') < src.index('. "$BOX_ENV"')


def test_bootstrap_sh_has_no_crlf():
    assert b"\r" not in _BOOTSTRAP_SH.read_bytes()


# --- P5-07 7c: pg_dump -Fc + pg_restore share the SAME connection target -----
def test_backup_uses_custom_format_dump(box):
    # A migration-crossing update dumps with -Fc (custom archive) so stock pg_restore
    # can consume it; the pg_dump stub records its args.
    box.set_serve(signed_serve(migration_from="0019", migration_to="0020", rollback_kind="restore_required"))
    box.set_alembic_current("0020")
    assert box.run().returncode == 0, "migration-crossing update should succeed"
    assert "pg_dump -Fc" in box.stub_log()


def test_update_sh_dump_and_restore_share_connection_target():
    """Static contract (7c/G2-2): the dump (-Fc) and restore (--clean --if-exists)
    resolve to the SAME connection target via the ONE shared $PG_CONN indirection — the
    dump is never left implicit while the restore is explicit — and neither targets a
    plain .sql created without -Fc."""
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert '"$PG_DUMP" -Fc -d "$PG_CONN"' in src                     # custom operator path
    assert '"$PG_RESTORE" --clean --if-exists -d "$PG_CONN"' in src  # same custom target
    assert "dc exec -T postgres pg_dump -U onebrain -Fc -d onebrain" in src
    assert "dc exec -T postgres pg_restore -U onebrain --clean --if-exists -d onebrain" in src
    # The dump/restore artifacts are custom-format archives (.dump), never a plain .sql
    # that stock pg_restore cannot read.
    assert 'DUMP="$WORK/backup.dump"' in src and 'RESTORE="$WORK/restore.dump"' in src


def test_migration_backup_keeps_postgres_up_and_recovers_failed_backup():
    src = _UPDATE_SH.read_text(encoding="utf-8")
    quiesce = src[src.index("quiesce_application_services()"):
                   src.index("resume_current_stack()")]
    assert "services=(caddy)" in quiesce
    assert "postgres" not in quiesce
    assert "redis" not in quiesce
    failure = src[src.index('log "backup FAILED; restoring current stack'):
                  src.index('# --- 5. PULL + UP')]
    assert "resume_current_stack" in failure


def test_verified_api_digest_also_pins_migration_service():
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert 'service_images["onebrain-migrate"] = selected["onebrain-api"]' in src


# --- P5-07 7d: box records a well-formed backup_manifest (A17 gate) -----------
def test_backup_manifest_recorded_on_migration_crossing_success(box):
    import re

    box.set_serve(signed_serve(migration_from="0019", migration_to="0020", rollback_kind="restore_required"))
    box.set_alembic_current("0020")
    assert box.run().returncode == 0
    state = box.state()
    assert state["backup_status"] == "success"
    # A well-formed sha256:<64hex>:<bytes> manifest of the ENCRYPTED backup object.
    assert re.match(r"^sha256:[0-9a-f]{64}:\d+$", state["backup_manifest"]), state["backup_manifest"]


def test_no_backup_manifest_without_schema_change(box):
    # No schema change -> no backup taken -> empty manifest (nothing for MC to net).
    box.set_serve(signed_serve(migration_from="0020", migration_to="0020"))
    assert box.run().returncode == 0
    state = box.state()
    assert state["outcome"] == "succeeded" and state["backup_manifest"] == ""


# --- shellcheck (CI-only; A4) ------------------------------------------------
def test_shellcheck_clean():
    checker = shutil.which("shellcheck")
    if checker is None:
        pytest.skip("shellcheck absent (CI-only gate; not a local merge gate — A4)")
    for script in (_UPDATE_SH, _BOOTSTRAP_SH):
        result = subprocess.run([checker, str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

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
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from app.trust.envelope import DesiredStateEnvelope, SignedReleaseBlock, sign_desired_state
from app.trust.release import canonical_release_payload
from app.trust.signing import generate_keypair, sign_payload

def _find_usable_bash() -> str | None:
    """Return Bash only when the discovered executable can actually run.

    Windows can expose ``bash.exe`` as the WSL launcher even when no Linux
    distribution is installed.  Treating that launcher as Bash makes every
    shell harness test fail before its first assertion.
    """

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
_BOX_DIR = Path(__file__).resolve().parents[1] / "deploy" / "box"
_UPDATE_SH = _BOX_DIR / "update.sh"
_BOOTSTRAP_SH = _BOX_DIR / "onebrain_bootstrap.sh"
_GATE_AGENT_SH = _BOX_DIR / "onebrain-gate-agent.sh"
_DOTENV_SH = _BOX_DIR / "onebrain_dotenv.sh"
_VERIFY_PY = _BOX_DIR / "onebrain_box_verify.py"

pytestmark = pytest.mark.skipif(_BASH is None, reason="functional bash unavailable (harness needs /usr/bin/bash)")

REL_PRIV, REL_PUB = generate_keypair()
DS_PRIV, DS_PUB = generate_keypair()
GOOD_IMG = "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64

_STUBS = {
    "docker": (
        '#!/usr/bin/env bash\n'
        'echo "docker $*" >> "$STUB_LOG"\n'
        'if [ "$1" = "pull" ]; then echo "$2" >> "$CTRL/pulled_refs"; fi\n'
        'if [ "$1" = "compose" ]; then\n'
        '  for a in "$@"; do\n'
        '    if [ "$a" = "stop" ]; then\n'
        '      if [ -f "$CTRL/compose_stop_fail_once" ]; then rm -f "$CTRL/compose_stop_fail_once"; exit 1; fi\n'
        '      [ -f "$CTRL/compose_stop_fail" ] && exit 1\n'
        '    fi\n'
        '    if [ "$a" = "up" ]; then\n'
        '      if [ -f "$CTRL/compose_up_fail_once" ]; then rm -f "$CTRL/compose_up_fail_once"; exit 1; fi\n'
        '      [ -f "$CTRL/compose_up_fail" ] && exit 1\n'
        '      if [ -f "$CTRL/compose_stop_fail_after_up" ]; then rm -f "$CTRL/compose_stop_fail_after_up"; touch "$CTRL/compose_stop_fail_once"; fi\n'
        '    fi\n'
        '  done\n'
        'fi\n'
        'if [[ "$*" == *"run --rm onebrain-migrate alembic current"* ]]; '
        'then cat "$CTRL/alembic_current" 2>/dev/null || true; fi\n'
        # Local image inventory for the post-success prune. With no control file
        # the listing is empty, so every other scenario keeps its old behaviour.
        'if [[ "$1" == "image" && "$2" == "ls" ]]; '
        'then cat "$CTRL/image_list" 2>/dev/null || true; fi\n'
        'if [ -f "$CTRL/activation_fail" ] && '
        '[[ "$*" == *"app.drive.malware.activation"* ]]; then exit 1; fi\n'
        'exit 0\n'
    ),
    "curl": (
        '#!/usr/bin/env bash\n'
        'echo "curl $*" >> "$STUB_LOG"\n'
        'url=""; out=""; want_code=0; prev=""\n'
        # onebrain_bootstrap.sh reads the status code via -o/-w so it can tell a
        # rejected credential from an unreachable control plane. Honour both.
        'for a in "$@"; do\n'
        '  case "$prev" in -o) out="$a" ;; esac\n'
        '  case "$a" in -w) want_code=1 ;; esac\n'
        '  prev="$a"; url="$a"\n'
        'done\n'
        'emit() { if [ -n "$out" ]; then printf "%s" "$1" > "$out"; else printf "%s" "$1"; fi; '
        '[ "$want_code" = "1" ] && printf "%s" "${2:-200}"; }\n'
        'case "$url" in\n'
        '  *floor-bump*) [ -f "$CTRL/floor_bump_fail" ] && exit 22; '
        'cat "$CTRL/floor_bump_serve.json" 2>/dev/null || printf "null" ;;\n'
        '  *bootstrap*) [ -f "$CTRL/bootstrap_fail" ] && exit 22; '
        'emit "$(cat "$CTRL/bootstrap_resp.json" 2>/dev/null || printf "")" '
        '"$(cat "$CTRL/bootstrap_http_code" 2>/dev/null || printf "200")" ;;\n'
        '  *desired-state*) [ -f "$CTRL/fetch_fail" ] && exit 22; cat "$CTRL/serve.json" ;;\n'
        '  *health*) if [ -f "$CTRL/smoke_fail_once" ]; then rm -f "$CTRL/smoke_fail_once"; exit 22; fi; '
        '[ -f "$CTRL/smoke_fail" ] && exit 22; printf "OK" ;;\n'
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
        '[ -f "$CTRL/pg_dump_fail" ] && exit 1\n'
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
    "onebrain-data-volume.sh": (
        '#!/usr/bin/env bash\n'
        '[ "${1:-}" = "verify" ] || exit 64\n'
        '[ -f "$CTRL/data_volume_fail" ] && exit 1\n'
        'exit 0\n'
    ),
    "mountpoint": (
        '#!/usr/bin/env bash\n'
        '[ "${1:-}" = "-q" ] || exit 64\n'
        '[ -f "$CTRL/mountpoint_fail" ] && exit 1\n'
        'exit 0\n'
    ),
    "readlink": (
        '#!/usr/bin/env bash\n'
        'path=""; for arg in "$@"; do path="$arg"; done\n'
        'printf "%s\\n" "$path"\n'
    ),
    "findmnt": (
        '#!/usr/bin/env bash\n'
        'kind=""; target=""; for arg in "$@"; do case "$arg" in SOURCE|TARGET) kind="$arg" ;; esac; target="$arg"; done\n'
        'case "$kind" in\n'
        '  SOURCE) if [ "$target" = "$ONEBRAIN_MAINTENANCE_DIR" ] && [ -f "$CTRL/maintenance_source_mismatch" ]; then printf "nested-source\\n"; else printf "verified-source\\n"; fi ;;\n'
        '  TARGET) if [ -f "$CTRL/maintenance_target_mismatch" ]; then printf "%s\\n" "$ONEBRAIN_MAINTENANCE_DIR"; else printf "%s\\n" "$ONEBRAIN_DATA_MOUNT"; fi ;;\n'
        '  *) exit 64 ;;\n'
        'esac\n'
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
        self.mount = root / "data-mount"
        self.data = self.mount / "onebrain-maintenance"
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
            "UPDATE_DATA_DIR": _unix(self.root / "app-data"),
            "ONEBRAIN_DATA_MOUNT": _unix(self.mount),
            "ONEBRAIN_MAINTENANCE_DIR": _unix(self.data),
            "ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT": _unix(self.bin / "onebrain-data-volume.sh"),
            "UPDATE_COMPOSE_DIR": _unix(self.root),
            "UPDATE_COMPOSE_PROJECT": "onebrain-dep_a",
            "UPDATE_PROFILES": "onebrain",
            "UPDATE_LOCAL_MODULES": "onebrain-api",
            "UPDATE_HEALTH_URL": "http://127.0.0.1/health",
            # General updater scenarios predate the role-split host assets.
            # Dedicated 0030 tests still force the migration's non-bypassable
            # preflight; rendered hosts set this true for successor migrations.
            "UPDATE_ROLE_SPLIT_REQUIRED": "false",
            "UPDATE_VERIFY_BIN": _unix(_VERIFY_PY),
            "UPDATE_BACKUP_KEY": "k" * 32,
            "UPDATE_RECOVERY_HEALTH_ATTEMPTS": "1",
            "UPDATE_RECOVERY_HEALTH_INTERVAL_SECONDS": "0",
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

    def seed_role_split_assets(self):
        """Write the minimal compatible host assets needed by the 0030 preflight."""
        init = self.root / "postgres-init.sh"
        init.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(init, 0o755)
        env_dir = self.root / "env"
        env_dir.mkdir(exist_ok=True)
        (env_dir / "onebrain-migrate.env").write_text(
            "ONEBRAIN_POSTGRES_APP_ROLE=onebrain_app\n"
            "ONEBRAIN_POSTGRES_WORKER_ROLE=onebrain_worker\n",
            encoding="utf-8",
        )
        (env_dir / "postgres.env").write_text(
            "POSTGRES_APP_ROLE=onebrain_app\n"
            "POSTGRES_WORKER_ROLE=onebrain_worker\n",
            encoding="utf-8",
        )
        (self.root / "docker-compose.yml").write_text(
            "services:\n"
            "  postgres-roles:\n"
            "    volumes:\n"
            "      - /opt/onebrain/postgres-init.sh:/opt/onebrain/postgres-init.sh:ro\n"
            "  onebrain-api:\n"
            "    image: ghcr.io/proark1/onebrain-api@sha256:" + "f" * 64 + "\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            "POSTGRES_PASSWORD=" + "o" * 32 + "\n"
            "POSTGRES_APP_PASSWORD=" + "a" * 32 + "\n"
            "POSTGRES_WORKER_PASSWORD=" + "w" * 32 + "\n"
            "POSTGRES_ASSISTANT_PASSWORD=" + "s" * 32 + "\n"
            "POSTGRES_COMMUNICATION_PASSWORD=" + "c" * 32 + "\n"
            "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET=" + "r" * 32 + "\n",
            encoding="utf-8",
        )

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
            "UPDATE_SCRIPT": _unix(_UPDATE_SH),
        }

    def _exec(self, script: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        cmd = f'export PATH="{_unix(self.bin)}:$PATH"; exec "{_unix(script)}"'
        return subprocess.run(
            [_BASH, "-c", cmd],
            env={**self._env(), **(extra_env or {})},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def run(self, **extra_env: str) -> subprocess.CompletedProcess:
        return self._exec(_UPDATE_SH, extra_env)

    def run_bootstrap(self, **extra_env: str) -> subprocess.CompletedProcess:
        return self._exec(_BOOTSTRAP_SH, extra_env)

    def run_gate_agent(self, **extra_env: str) -> subprocess.CompletedProcess:
        return self._exec(_GATE_AGENT_SH, extra_env)

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
    last_applied = json.loads(
        (box.data / "onebrain_update" / "last_applied.json").read_text(encoding="utf-8")
    )
    assert last_applied["modules"] == {"onebrain-api": "2026.7.2"}


def test_verified_update_prunes_superseded_images(box):
    """Images from retired releases are reclaimed once the new one is healthy.

    update.sh pulls a digest-pinned set every release and used to remove none of
    them, so /var/lib/containerd grew until the root disk filled and the box
    could no longer start a container (2026-07-20: the development gate died at
    0 bytes free).
    """
    stale = "ghcr.io/proark1/onebrain-api@sha256:" + "9" * 64
    (box.ctrl / "image_list").write_bytes(f"{stale}\n{GOOD_IMG}\n".encode("utf-8"))
    box.set_serve(signed_serve(migration_from="0020", migration_to="0020"))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    assert f"image rm {stale}" in box.stub_log()


def test_prune_never_removes_the_rollback_or_current_images(box):
    """Reclaiming disk must never destroy the path back to the previous release.

    The rollback override is not referenced by any running container, so an
    unguarded `docker image prune -a` would delete exactly the images recovery
    depends on -- strictly worse than the full disk it is meant to prevent.
    """
    previous = "ghcr.io/proark1/onebrain-api@sha256:" + "b" * 64
    # Seed the CURRENT override: install_next_override rotates it to .prev during
    # the update, which is what actually makes it the rollback target. Writing
    # .prev directly would be unrealistic -- the update overwrites it.
    (box.root / "images.override.yml").write_text(
        f"services:\n  onebrain-api:\n    image: {previous}\n", encoding="utf-8"
    )
    (box.ctrl / "image_list").write_bytes(f"{previous}\n{GOOD_IMG}\n".encode("utf-8"))
    box.set_serve(signed_serve(migration_from="0020", migration_to="0020"))

    result = box.run()

    assert result.returncode == 0, result.stderr
    log = box.stub_log()
    assert f"image rm {previous}" not in log      # the rollback path survives
    assert f"image rm {GOOD_IMG}" not in log      # so does the release just installed


def test_prune_protects_a_kept_image_pinned_with_a_tag_and_a_digest(box):
    """The keep comparison is on the digest, because references do not survive.

    `docker image ls --format '{{.Repository}}@{{.Digest}}'` drops the tag, so an
    image pinned `name:tag@sha256:...` -- the form render.py already uses for
    caddy and redis -- lists as `name@sha256:...`. Comparing whole references
    fails toward deletion: the live image stops matching its own keep entry, and
    survives only because `image rm` is unforced and a container holds it.
    """
    kept = "ghcr.io/proark1/onebrain-api@sha256:" + "c" * 64
    (box.root / "images.override.yml").write_text(
        # Same digest as `kept`, written with a tag as well.
        "services:\n  onebrain-api:\n    image: "
        "ghcr.io/proark1/onebrain-api:2026.7.2@sha256:" + "c" * 64 + "\n",
        encoding="utf-8",
    )
    (box.ctrl / "image_list").write_bytes(f"{kept}\n{GOOD_IMG}\n".encode("utf-8"))
    box.set_serve(signed_serve(migration_from="0020", migration_to="0020"))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert f"image rm {kept}" not in box.stub_log()


def test_prune_is_skipped_when_no_protected_digests_resolve(box):
    """Fail closed: an empty keep set must authorise no deletion at all."""
    stale = "ghcr.io/proark1/onebrain-api@sha256:" + "9" * 64
    (box.ctrl / "image_list").write_bytes(f"{stale}\n".encode("utf-8"))
    box.set_serve(signed_serve(migration_from="0020", migration_to="0020"))
    result = box.run()
    assert result.returncode == 0, result.stderr
    # The override always names the installed release, so the keep set is never
    # empty on a real success -- and the stale image is still reclaimed.
    assert f"image rm {stale}" in box.stub_log()


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


@pytest.mark.parametrize(
    "images_field",
    [pytest.param({}, id="missing"), pytest.param({"images": None}, id="null")],
)
def test_legacy_last_applied_images_shape_does_not_kill_update_agent(box, images_field):
    work = box.data / "onebrain_update"
    work.mkdir(parents=True, exist_ok=True)
    (work / "last_applied.json").write_text(
        json.dumps({
            "version": "2026.07.18.223",
            "migration_to": "0030_job_queue_rls_roles",
            "modules": {"onebrain-api": "2026.07.18.223"},
            **images_field,
        }),
        encoding="utf-8",
    )
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.pulled() == [GOOD_IMG]
    assert box.state()["outcome"] == "succeeded"


def test_current_digest_and_migration_acknowledge_exact_attempt_without_restart(box):
    box.seed_last_applied({"onebrain-api": GOOD_IMG})
    box.set_alembic_current("0020")
    box.set_serve(signed_serve(attempt_id="roll_retry"))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.pulled() == []
    assert " up -d" not in box.stub_log()
    state = box.state()
    assert state["ts"]
    assert {key: value for key, value in state.items() if key != "ts"} == {
        "last_target_version": "2026.7.2",
        "outcome": "succeeded",
        "migration_reached": "0020",
        "attempt_id": "roll_retry",
        "backup_status": "",
        "backup_ts": "",
        "backup_manifest": "",
    }
    last_applied = json.loads(
        (box.data / "onebrain_update" / "last_applied.json").read_text(encoding="utf-8")
    )
    assert last_applied["version"] == "2026.7.2"
    assert last_applied["images"] == {"onebrain-api": GOOD_IMG}


def test_current_digest_with_migration_drift_continues_apply(box):
    box.seed_last_applied({"onebrain-api": GOOD_IMG})
    box.set_alembic_current("0020")
    box.set_serve(signed_serve(migration_from="0020", migration_to="0021"))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.pulled() == [GOOD_IMG]
    assert box.state()["outcome"] == "rolled_back"


def test_current_digest_without_migration_contract_acknowledges_attempt(box):
    box.seed_last_applied({"onebrain-api": GOOD_IMG})
    box.set_serve(signed_serve(
        migration_from="",
        migration_to="",
        attempt_id="roll_no_migration",
    ))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.pulled() == []
    assert " up -d" not in box.stub_log()
    assert box.state()["outcome"] == "succeeded"
    assert box.state()["attempt_id"] == "roll_no_migration"
    assert box.state()["migration_reached"] == ""


def test_current_digest_does_not_bypass_failed_bundle_refresh(box):
    box.seed_last_applied({"onebrain-api": GOOD_IMG})
    box.set_alembic_current("0020")
    box.set_serve(signed_serve(attempt_id="roll_bundle_failed"))

    result = box.run(UPDATE_BUNDLE_REFRESH_FAILED="true")

    assert result.returncode == 0, result.stderr
    assert box.pulled() == []
    assert " up -d" not in box.stub_log()
    assert box.state()["outcome"] == "failed"
    assert box.state()["attempt_id"] == "roll_bundle_failed"


def test_restore_required_current_digest_still_records_backup_evidence(box):
    box.seed_last_applied({"onebrain-api": GOOD_IMG})
    box.set_alembic_current("0020")
    box.set_serve(signed_serve(rollback_kind="restore_required", attempt_id="roll_restore_retry"))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.pulled() == [GOOD_IMG]
    assert "pg_dump" in box.stub_log()
    state = box.state()
    assert state["outcome"] == "succeeded"
    assert state["attempt_id"] == "roll_restore_retry"
    assert state["backup_status"] == "success"
    assert re.match(r"^sha256:[0-9a-f]{64}:\d+$", state["backup_manifest"])


def test_verify_failure_holds(box):
    box.set_serve(signed_serve(tamper_wrapper=True))
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.pulled() == []                       # NO pull on a rejected envelope
    assert box.state()["outcome"] == "failed"
    assert "envelope_signature_invalid" in box.stub_log() or \
        "envelope_signature_invalid" in (box.data / "onebrain_update" / "update.log").read_text()


def test_update_holds_without_the_verified_maintenance_volume(box):
    box.touch("data_volume_fail")
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state() is None
    assert box.pulled() == []
    assert "persistent data volume is unavailable or mismatched" in result.stderr


def test_update_holds_when_maintenance_subtree_is_a_different_mount(box):
    box.touch("maintenance_source_mismatch")
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state() is None
    assert box.pulled() == []
    assert "maintenance directory is not on the verified data volume" in result.stderr


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


def test_migration_crossing_activates_drive_quarantine_before_services_start(box):
    box.set_serve(
        signed_serve(
            migration_from="0033_onebrain_drive",
            migration_to="0034_drive_malware_quarantine",
            rollback_kind="restore_required",
        )
    )
    box.set_alembic_current("0034_drive_malware_quarantine")

    result = box.run()

    assert result.returncode == 0, result.stderr
    log = box.stub_log()
    activation = log.index(
        "run --rm onebrain-migrate python -m app.drive.malware.activation --migrate"
    )
    service_start = log.index("up -d", activation)
    assert activation < service_start
    assert box.state()["outcome"] == "succeeded"


def test_activation_failure_restores_previous_stack(box):
    previous = (
        "services:\n  onebrain-api:\n    image: "
        + "ghcr.io/proark1/onebrain-api@sha256:"
        + "d" * 64
        + "\n"
    )
    (box.root / "images.override.yml").write_text(previous, encoding="utf-8")
    box.set_serve(
        signed_serve(
            migration_from="0033_onebrain_drive",
            migration_to="0034_drive_malware_quarantine",
            rollback_kind="restore_required",
        )
    )
    box.set_alembic_current("0034_drive_malware_quarantine")
    box.touch("activation_fail")

    result = box.run()

    assert result.returncode == 0, result.stderr
    log = box.stub_log()
    activation = log.index(
        "run --rm onebrain-migrate python -m app.drive.malware.activation --migrate"
    )
    assert log.index("pg_restore", activation) > activation
    assert log.index("up -d", activation) > activation
    assert box.state()["outcome"] == "rolled_back"
    assert (box.root / "images.override.yml").read_text(encoding="utf-8") == previous


def test_migration_crossing_fence_mismatch_restores_previous_stack(box):
    previous = "services:\n  onebrain-api:\n    image: ghcr.io/proark1/onebrain-api@sha256:" + "b" * 64 + "\n"
    (box.root / "images.override.yml").write_text(previous, encoding="utf-8")
    box.set_serve(signed_serve(migration_from="0019", migration_to="0020"))
    box.set_alembic_current("0019")                 # migrate did NOT reach target -> fence fails
    result = box.run()
    assert result.returncode == 0, result.stderr
    state = box.state()
    assert state["outcome"] == "rolled_back"
    assert state["migration_reached"] == "0019"
    assert (box.root / "images.override.yml").read_text(encoding="utf-8") == previous
    assert box.stub_log().count(" up -d") >= 2       # candidate, then restored stack


# --- smoke-fail recovery -----------------------------------------------------
def test_smoke_wait_retries_transient_candidate_health_failure(box):
    # Candidate services can need a moment after compose reports them started.
    # A single failed probe must not roll back if the bounded retry succeeds.
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8").replace(
            "UPDATE_RECOVERY_HEALTH_ATTEMPTS=1", "UPDATE_RECOVERY_HEALTH_ATTEMPTS=2"
        ),
        encoding="utf-8",
    )
    box.set_serve(signed_serve(rollback_kind="code_only"))
    box.touch("smoke_fail_once")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    assert box.stub_log().count("curl -sf http://127.0.0.1/health") == 2


def test_smoke_fail_code_only_rolls_back(box):
    box.set_serve(signed_serve(rollback_kind="code_only", migration_from="0020", migration_to="0020"))
    box.touch("smoke_fail_once")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "rolled_back"
    assert "pg_restore" not in box.stub_log()       # code_only never restores the DB


def test_smoke_fail_restore_required_restores(box):
    box.set_serve(signed_serve(rollback_kind="restore_required", migration_from="0019", migration_to="0020"))
    box.set_alembic_current("0020")
    box.touch("smoke_fail_once")
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


def test_candidate_start_failure_restores_previous_override(box):
    previous = "services:\n  onebrain-api:\n    image: ghcr.io/proark1/onebrain-api@sha256:" + "c" * 64 + "\n"
    (box.root / "images.override.yml").write_text(previous, encoding="utf-8")
    box.set_serve(signed_serve())
    box.touch("compose_up_fail_once")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "rolled_back"
    assert (box.root / "images.override.yml").read_text(encoding="utf-8") == previous
    assert box.stub_log().count(" up -d") >= 2


def test_recovery_health_failure_is_reported_as_failed(box):
    box.set_serve(signed_serve())
    box.touch("smoke_fail")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"


def test_role_split_preflight_holds_before_quiesce_or_pull(box):
    box.set_serve(signed_serve(
        migration_from="0029_auth_rate_limits",
        migration_to="0030_job_queue_rls_roles",
    ))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert box.pulled() == []
    assert " stop " not in box.stub_log()
    assert "role-split preflight failed" in (box.data / "onebrain_update" / "update.log").read_text()


def test_role_split_preflight_accepts_compatible_assets_and_credentials(box):
    box.seed_role_split_assets()
    box.set_serve(signed_serve(
        migration_from="0029_auth_rate_limits",
        migration_to="0030_job_queue_rls_roles",
    ))
    box.set_alembic_current("0030_job_queue_rls_roles")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    assert box.pulled() == [GOOD_IMG]


@pytest.mark.parametrize("missing_secret", [
    "POSTGRES_APP_PASSWORD",
    "POSTGRES_WORKER_PASSWORD",
    "POSTGRES_ASSISTANT_PASSWORD",
    "POSTGRES_COMMUNICATION_PASSWORD",
    "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",
])
def test_role_split_preflight_holds_when_any_runtime_secret_is_missing(box, missing_secret):
    box.seed_role_split_assets()
    env_file = box.root / ".env"
    env_file.write_text(
        "\n".join(
            line for line in env_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{missing_secret}=")
        ) + "\n",
        encoding="utf-8",
    )
    box.set_serve(signed_serve(
        migration_from="0029_auth_rate_limits",
        migration_to="0030_job_queue_rls_roles",
    ))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert box.pulled() == []
    assert " stop " not in box.stub_log()
    assert "role credentials are unavailable" in (box.data / "onebrain_update" / "update.log").read_text()


def test_role_split_preflight_runs_before_quiesce():
    src = _UPDATE_SH.read_text(encoding="utf-8")
    preflight = src.index("if requires_role_split_preflight && ! role_split_preflight; then")
    quiesce = src.index("quiesce_application_services >>", preflight)
    assert preflight < quiesce


def test_quiesce_failure_holds_before_migration_backup_or_candidate_pull(box):
    box.set_serve(signed_serve(
        migration_from="0019",
        migration_to="0020",
        rollback_kind="restore_required",
    ))
    box.touch("compose_stop_fail")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert box.pulled() == []
    assert "pg_dump" not in box.stub_log()
    assert "quiesce FAILED; holding current stack before backup" in (
        box.data / "onebrain_update" / "update.log"
    ).read_text()


def test_migration_holds_before_quiesce_when_backup_key_is_short(box):
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8").replace(
            "UPDATE_BACKUP_KEY=" + "k" * 32,
            "UPDATE_BACKUP_KEY=too-short",
        ),
        encoding="utf-8",
    )
    box.set_serve(signed_serve(
        migration_from="0019",
        migration_to="0020",
        rollback_kind="restore_required",
    ))

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert box.pulled() == []
    assert " stop " not in box.stub_log()
    assert "pg_dump" not in box.stub_log()
    assert "backup key is unavailable or too short" in (
        box.data / "onebrain_update" / "update.log"
    ).read_text()


def test_update_removes_partial_plaintext_backup_after_backup_failure(box):
    box.set_serve(signed_serve(
        migration_from="0019",
        migration_to="0020",
        rollback_kind="restore_required",
    ))
    box.touch("pg_dump_fail")

    result = box.run()

    assert result.returncode == 0, result.stderr
    work = box.data / "onebrain_update"
    assert box.state()["outcome"] == "failed"
    assert not (work / "backup.dump").exists()
    assert not (work / "restore.dump").exists()


def test_encrypted_backup_retention_prunes_old_archives_but_keeps_newest(box):
    backups = box.data / "backups"
    backups.mkdir(parents=True)
    old_archive = backups / "backup-old.dump.enc"
    old_archive.write_text("old", encoding="utf-8")
    old_epoch = time.time() - 90 * 24 * 60 * 60
    os.utime(old_archive, (old_epoch, old_epoch))
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8") + "UPDATE_BACKUP_RETENTION_DAYS=1\n",
        encoding="utf-8",
    )
    box.set_serve(signed_serve(
        migration_from="0019",
        migration_to="0020",
        rollback_kind="restore_required",
    ))
    box.set_alembic_current("0020")

    result = box.run()

    assert result.returncode == 0, result.stderr
    archives = list(backups.glob("backup-*.dump.enc"))
    assert old_archive not in archives
    assert archives, "the newest encrypted rollback archive must be retained"
    source = _UPDATE_SH.read_text(encoding="utf-8")
    assert '[ "$archive" -ef "$newest" ] && continue' in source


def test_update_reclaims_a_stale_dead_lock(box):
    lock = box.data / "onebrain_update" / "update.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("not-a-pid\n", encoding="utf-8")
    (lock / "started_at").write_text("0\n", encoding="utf-8")
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8") + "UPDATE_LOCK_STALE_SECONDS=0\n",
        encoding="utf-8",
    )
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    assert not lock.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process liveness assertion")
def test_update_keeps_a_live_lock_even_when_its_timestamp_is_old(box):
    lock = box.data / "onebrain_update" / "update.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (lock / "started_at").write_text("0\n", encoding="utf-8")
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8") + "UPDATE_LOCK_STALE_SECONDS=0\n",
        encoding="utf-8",
    )
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state() is None
    assert "/api/fleet/desired-state" not in box.stub_log()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode assertion")
def test_update_scratch_is_owner_only(box):
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    work = box.data / "onebrain_update"
    assert stat.S_IMODE(work.stat().st_mode) == 0o700


def test_restore_recovery_does_not_restore_while_candidate_quiesce_fails(box):
    box.set_serve(signed_serve(
        migration_from="0019",
        migration_to="0020",
        rollback_kind="restore_required",
    ))
    box.set_alembic_current("0020")
    box.touch("smoke_fail_once")
    box.touch("compose_stop_fail_after_up")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert "pg_restore" not in box.stub_log()
    assert "candidate quiesce FAILED; cannot restore database" in (
        box.data / "onebrain_update" / "update.log"
    ).read_text()


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


def _callback_agent_stub(tmp_path):
    """Stand in for onebrain-gate-agent.sh --provision-callback."""
    agent = tmp_path / "gate-agent-stub.sh"
    agent.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s %s\\n" "$1" "$ONEBRAIN_CALLBACK_STATUS" >> "$CTRL/provision_callback"\n',
        encoding="utf-8",
    )
    agent.chmod(0o755)
    return agent


def _with_callback_token(box):
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8") + "ONEBRAIN_PROVISIONING_CALLBACK_TOKEN=cbt_test\n",
        encoding="utf-8",
    )


def test_bootstrap_reports_a_rejected_first_boot_token_to_mission_control(box, tmp_path):
    """A 401 is terminal: a single-use first-boot token cannot be reissued.

    Holding silently is what left a dead box reporting `dispatched` with every
    module `active` while it served 502 (2026-07-20). The run must fail loudly.
    """
    _with_callback_token(box)
    agent = _callback_agent_stub(tmp_path)
    (box.ctrl / "bootstrap_http_code").write_text("401", encoding="utf-8")
    box.set_bootstrap_resp({"detail": "Invalid, expired, or consumed bootstrap token."})

    result = box.run_bootstrap(ONEBRAIN_GATE_AGENT=_unix(agent))

    # Reporting must not itself break cloud-init, and no bundle was written.
    assert result.returncode == 0, result.stderr
    assert box.env_content() is None
    assert (box.ctrl / "provision_callback").read_text().splitlines() == [
        "--provision-callback failed"
    ]

    # The run is terminal, so a later timer tick must not report it again.
    box.run_bootstrap(ONEBRAIN_GATE_AGENT=_unix(agent))
    assert (box.ctrl / "provision_callback").read_text().splitlines() == [
        "--provision-callback failed"
    ]


def test_bootstrap_holds_quietly_when_the_control_plane_is_unreachable(box, tmp_path):
    """Unreachable is retryable — failing the run would be wrong."""
    _with_callback_token(box)
    agent = _callback_agent_stub(tmp_path)
    box.touch("bootstrap_fail")

    result = box.run_bootstrap(ONEBRAIN_GATE_AGENT=_unix(agent))

    assert result.returncode == 0, result.stderr
    assert not (box.ctrl / "provision_callback").exists()


def test_bootstrap_rotation_failure_is_not_a_provisioning_failure(box, tmp_path):
    """A working box whose rotation is rejected has already provisioned."""
    _with_callback_token(box)
    agent = _callback_agent_stub(tmp_path)
    (box.root / ".env").write_text("ONEBRAIN_FLEET_KEY=fk_real\n", encoding="utf-8")
    (box.ctrl / "bootstrap_http_code").write_text("401", encoding="utf-8")

    result = box.run_bootstrap(ONEBRAIN_GATE_AGENT=_unix(agent))

    assert result.returncode == 0, result.stderr
    assert not (box.ctrl / "provision_callback").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode assertion")
def test_bootstrap_secret_work_files_are_owner_only(box):
    box.set_bootstrap_resp({"secrets_epoch": 0, "dotenv": "POSTGRES_PASSWORD=pg\n"})

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    work = box.data / "onebrain_update"
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    assert stat.S_IMODE((work / "bootstrap_resp.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((work / "secrets_epoch").stat().st_mode) == 0o600
    assert stat.S_IMODE((box.root / ".env").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode assertion")
def test_bootstrap_tightens_existing_secret_work_files(box):
    work = box.data / "onebrain_update"
    work.mkdir(parents=True)
    work.chmod(0o755)
    for filename in ("bootstrap_resp.json", "env.new", "secrets_epoch"):
        path = work / filename
        path.write_text("old\n", encoding="utf-8")
        path.chmod(0o644)
    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "POSTGRES_PASSWORD=pg\n"})

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    assert stat.S_IMODE((work / "bootstrap_resp.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((work / "secrets_epoch").stat().st_mode) == 0o600
    assert stat.S_IMODE((box.root / ".env").stat().st_mode) == 0o600


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


def test_bootstrap_rotation_retries_pending_epoch_after_compose_failure(box):
    box.set_bootstrap_resp({"secrets_epoch": 0, "dotenv": "POSTGRES_PASSWORD=v0\n"})
    assert box.run_bootstrap().returncode == 0
    assert box.applied_epoch() == "0"

    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "POSTGRES_PASSWORD=v1\n"})
    box.touch("compose_up_fail_once")
    first_attempt = box.run_bootstrap()

    assert first_attempt.returncode == 0, first_attempt.stderr
    assert "POSTGRES_PASSWORD=v1" in (box.env_content() or "")
    assert box.applied_epoch() == "0", "a failed compose reapply must not mark the epoch applied"

    retry = box.run_bootstrap()

    assert retry.returncode == 0, retry.stderr
    assert box.applied_epoch() == "1"
    assert box.stub_log().count(" up -d") >= 2


def test_bootstrap_rotation_preserves_digest_pinned_override(box):
    (box.root / ".env").write_text("ONEBRAIN_FLEET_KEY=fk_old\n", encoding="utf-8")
    work = box.data / "onebrain_update"
    work.mkdir(parents=True, exist_ok=True)
    (work / "secrets_epoch").write_text("0\n", encoding="utf-8")
    override = box.root / "images.override.yml"
    pinned = "services:\n  onebrain-api:\n    image: ghcr.io/proark1/onebrain-api@sha256:" + "d" * 64 + "\n"
    override.write_text(pinned, encoding="utf-8")
    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "ONEBRAIN_FLEET_KEY=fk_new\n"})

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    assert override.read_text(encoding="utf-8") == pinned
    assert f"-f {_unix(override)}" in box.stub_log()


def test_gate_agent_refreshes_bundle_before_desired_state(box):
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8").replace(
            "ONEBRAIN_FLEET_KEY=fk_test", "ONEBRAIN_FLEET_KEY=${ONEBRAIN_FLEET_KEY}"
        ),
        encoding="utf-8",
    )
    box.set_bootstrap_resp({"secrets_epoch": 0, "dotenv": "ONEBRAIN_FLEET_KEY=fk_refreshed\n"})
    box.set_serve(signed_serve())

    result = box.run_gate_agent()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    log = box.stub_log()
    assert log.index("/api/fleet/bootstrap") < log.index("/api/fleet/desired-state")
    assert "Authorization: Bearer fk_refreshed" in log


def test_gate_agent_skips_bootstrap_when_customer_refresh_is_not_required(box):
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8").replace(
            "ONEBRAIN_BOOTSTRAP_TOKEN=bt_harness_token",
            "ONEBRAIN_BOOTSTRAP_TOKEN=",
        ),
        encoding="utf-8",
    )
    box.set_serve(signed_serve())

    result = box.run_gate_agent()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    assert "/api/fleet/bootstrap" not in box.stub_log()


def test_gate_agent_reports_failed_bundle_refresh_without_starting_candidate(box):
    box.set_serve(signed_serve())

    result = box.run_gate_agent()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert box.pulled() == []
    log = box.stub_log()
    assert log.index("/api/fleet/bootstrap") < log.index("/api/fleet/desired-state")


def test_gate_agent_holds_customer_candidate_when_bootstrap_helper_is_missing(box):
    box.set_serve(signed_serve())
    missing = _unix(box.root / "missing-bootstrap.sh")

    result = box.run_gate_agent(ONEBRAIN_BOOTSTRAP_SCRIPT=missing)

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "failed"
    assert box.pulled() == []
    assert "/api/fleet/bootstrap" not in box.stub_log()


def test_bootstrap_invalid_served_dotenv_keeps_existing_bundle(box):
    (box.root / ".env").write_text("ONEBRAIN_FLEET_KEY=fk_old\n", encoding="utf-8")
    work = box.data / "onebrain_update"
    work.mkdir(parents=True, exist_ok=True)
    (work / "secrets_epoch").write_text("0\n", encoding="utf-8")
    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "1INVALID=do-not-install\n"})

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    assert box.env_content() == "ONEBRAIN_FLEET_KEY=fk_old\n"
    assert box.applied_epoch() == "0"
    assert "compose" not in box.stub_log()


def test_bootstrap_holds_without_the_verified_maintenance_volume(box):
    box.touch("data_volume_fail")
    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "ONEBRAIN_FLEET_KEY=fk_new\n"})

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    assert box.env_content() is None
    assert box.applied_epoch() is None
    assert "persistent data volume is unavailable or mismatched" in result.stderr


def test_update_literal_dotenv_is_not_evaluated_and_box_refs_expand(box):
    marker = box.ctrl / "dotenv-executed"
    literal = 'literal#=x$(touch "$CTRL/dotenv-executed")'
    (box.root / ".env").write_text(f"ONEBRAIN_FLEET_KEY={literal}\n", encoding="utf-8")
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8").replace(
            "ONEBRAIN_FLEET_KEY=fk_test", "ONEBRAIN_FLEET_KEY=${ONEBRAIN_FLEET_KEY}"
        ),
        encoding="utf-8",
    )
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert box.state()["outcome"] == "succeeded"
    assert f"Authorization: Bearer {literal}" in box.stub_log()


def test_update_accepts_crlf_dotenv_with_blank_lines(box):
    # Existing boxes may retain a Windows-authored dotenv.  The literal loader
    # must normalize only CRLF terminators, skip blank lines, and leave values
    # unevaluated before box.env expands the fleet key reference.
    literal = "fk_crlf_compatible"
    (box.root / ".env").write_bytes(
        b"\r\nONEBRAIN_FLEET_KEY=" + literal.encode("ascii") + b"\r\n\r\n"
    )
    box_env = box.root / "box.env"
    box_env.write_text(
        box_env.read_text(encoding="utf-8").replace(
            "ONEBRAIN_FLEET_KEY=fk_test", "ONEBRAIN_FLEET_KEY=${ONEBRAIN_FLEET_KEY}"
        ),
        encoding="utf-8",
    )
    box.set_serve(signed_serve())

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state()["outcome"] == "succeeded"
    assert f"Authorization: Bearer {literal}" in box.stub_log()
    assert "\r" not in box.stub_log()


def test_bootstrap_rotation_loads_existing_dotenv_literally(box):
    marker = box.ctrl / "bootstrap-dotenv-executed"
    literal = 'literal#=x$(touch "$CTRL/bootstrap-dotenv-executed")'
    (box.root / ".env").write_text(f"ONEBRAIN_FLEET_KEY={literal}\n", encoding="utf-8")
    box.set_bootstrap_resp({"secrets_epoch": 1, "dotenv": "POSTGRES_PASSWORD=pg\n"})

    result = box.run_bootstrap()

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert box.applied_epoch() == "1"
    assert f"Authorization: Bearer {literal}" in box.stub_log()


def test_invalid_dotenv_holds_before_network_or_docker_work(box):
    (box.root / ".env").write_text("1INVALID=do-not-leak\n", encoding="utf-8")

    result = box.run()

    assert result.returncode == 0, result.stderr
    assert box.state() is None
    assert box.stub_log() == ""
    assert "do-not-leak" not in result.stdout + result.stderr


def test_update_sh_loads_literal_env_before_box_env():
    # The exchanged Docker Compose bundle is literal-loaded BEFORE renderer-owned
    # box.env expands its ${VAR} references (P5-03).
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert src.index('onebrain_load_dotenv "$ENV_FILE"') < src.index('. "$BOX_ENV"')
    assert '. "$ENV_FILE"' not in src


def test_gate_agent_loads_literal_env_before_box_env():
    src = _GATE_AGENT_SH.read_text(encoding="utf-8")
    assert src.index('onebrain_load_dotenv "$ENV_FILE"') < src.index('. "$BOX_ENV"')
    assert '. "$ENV_FILE"' not in src


def test_bootstrap_sh_has_no_crlf():
    for script in (_BOOTSTRAP_SH, _GATE_AGENT_SH, _DOTENV_SH):
        assert b"\r" not in script.read_bytes()


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
    assert "recover_current_stack" in failure


def test_verified_api_digest_also_pins_migration_service():
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert 'service_images["onebrain-migrate"] = selected["onebrain-api"]' in src


def test_update_override_backfills_persistent_definition_cache_for_existing_boxes():
    src = _UPDATE_SH.read_text(encoding="utf-8")
    assert 'MALWARE_DEFINITION_CACHE_DIR="/var/lib/onebrain/clamav"' in src
    assert (
        'install -d -o 10001 -g 10001 -m 0700 "$MALWARE_DEFINITION_CACHE_DIR"'
        in src
    )
    assert (
        'lines.append("      - /var/lib/onebrain/clamav:/var/lib/onebrain/clamav")'
        in src
    )


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
    for script in (_UPDATE_SH, _BOOTSTRAP_SH, _GATE_AGENT_SH, _DOTENV_SH):
        result = subprocess.run([checker, str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


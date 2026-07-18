"""Register the exact digest-pinned output of a green main build with Mission Control.

This script intentionally knows only the development signing key and the narrowly
scoped candidate bearer credential. It has no production-key input.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.controlplane.base import ReleaseManifest, validate_image_ref
from app.controlplane.migration_lint import classify_release
from app.trust.release import release_signature_fields, sign_release


MODULE_REPOSITORIES = {
    "onebrain-api": "ghcr.io/proark1/onebrain-api",
    "onebrain-workers": "ghcr.io/proark1/onebrain-workers",
    "onebrain-admin-ui": "ghcr.io/proark1/onebrain-admin-ui",
}

EXTERNAL_MODULE_INPUTS = {
    "assistant-service": (
        "ONEBRAIN_ASSISTANT_IMAGE_REF",
        "ONEBRAIN_ASSISTANT_REVISION",
    ),
    "communication-api": (
        "ONEBRAIN_COMMUNICATION_IMAGE_REF",
        "ONEBRAIN_COMMUNICATION_REVISION",
    ),
    "communication-widget": (
        "ONEBRAIN_COMMUNICATION_IMAGE_REF",
        "ONEBRAIN_COMMUNICATION_REVISION",
    ),
    "communication-voice": (
        "ONEBRAIN_COMMUNICATION_IMAGE_REF",
        "ONEBRAIN_COMMUNICATION_REVISION",
    ),
    "communication-workers": (
        "ONEBRAIN_COMMUNICATION_IMAGE_REF",
        "ONEBRAIN_COMMUNICATION_REVISION",
    ),
}


def candidate_version(run_number: str, now: datetime | None = None) -> str:
    clock = now or datetime.now(timezone.utc)
    if not run_number.isdigit() or int(run_number) < 1:
        raise ValueError("GITHUB_RUN_NUMBER must be a positive integer")
    return f"{clock:%Y.%m.%d}.{int(run_number)}"


def _post_json(
    url: str,
    payload: dict,
    *,
    key_id: str,
    secret: str,
    opener=urlopen,
) -> dict:
    request = Request(
        url,
        method="POST",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "X-OneBrain-Candidate-Key-Id": key_id,
        },
    )
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Mission Control rejected candidate ({exc.code}): {detail}") from exc


def _required(env: dict[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def classify_current_migration_delta(env: dict[str, str]) -> str:
    base = (env.get("GITHUB_EVENT_BEFORE") or "").strip()
    if not base or set(base) == {"0"}:
        base = "HEAD^"
    command = [
        "git", "diff", "--name-only", "--diff-filter=A", base, "HEAD", "--",
        "migrations/versions/*.py",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError("could not determine the migration delta for this candidate")
    sources = []
    for name in result.stdout.splitlines():
        path = Path(name.strip())
        if path.is_file():
            sources.append((path.as_posix(), path.read_text(encoding="utf-8")))
    return classify_release(alembic_sources=sources).rollback_kind


def current_alembic_head(root: Path = Path("migrations/versions")) -> str:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                        try:
                            values[target.id] = ast.literal_eval(value)
                        except (ValueError, TypeError):
                            pass
        revision = values.get("revision")
        down_revision = values.get("down_revision")
        if isinstance(revision, str):
            revisions.add(revision)
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif isinstance(down_revision, (tuple, list)):
            parents.update(value for value in down_revision if isinstance(value, str))
    heads = revisions - parents
    if len(heads) != 1:
        raise ValueError(f"expected one Alembic head, found {sorted(heads)}")
    return next(iter(heads))


def register_from_environment(env: dict[str, str] | None = None, *, opener=urlopen) -> dict:
    values = dict(os.environ if env is None else env)
    # Fail loudly if somebody tries to broaden this job's custody later.
    forbidden = [
        name for name in ("ONEBRAIN_RELEASE_PRIVATE_KEY", "ONEBRAIN_PRODUCTION_RELEASE_PRIVATE_KEY")
        if values.get(name)
    ]
    if forbidden:
        raise ValueError(f"production private keys are forbidden in candidate registration: {forbidden}")

    base_url = _required(values, "ONEBRAIN_MC_URL").rstrip("/")
    key_id = _required(values, "ONEBRAIN_RELEASE_CANDIDATE_KEY_ID")
    secret = _required(values, "ONEBRAIN_RELEASE_CANDIDATE_SECRET")
    dev_private_key = _required(values, "ONEBRAIN_DEV_RELEASE_PRIVATE_KEY")
    git_sha = _required(values, "GITHUB_SHA")
    version = (values.get("ONEBRAIN_CANDIDATE_VERSION") or "").strip()
    if not version:
        version_clock = None
        candidate_date = (values.get("ONEBRAIN_CANDIDATE_DATE") or "").strip()
        if candidate_date:
            try:
                version_clock = datetime.fromisoformat(candidate_date.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("ONEBRAIN_CANDIDATE_DATE must be an ISO timestamp") from exc
        version = candidate_version(_required(values, "GITHUB_RUN_NUMBER"), version_clock)

    images: dict[str, str] = {}
    modules: dict[str, str] = {}
    for module_id, repository in MODULE_REPOSITORIES.items():
        env_name = f"ONEBRAIN_{module_id.upper().replace('-', '_')}_DIGEST"
        digest = _required(values, env_name)
        if not digest.startswith("sha256:"):
            raise ValueError(f"{env_name} is not a sha256 digest")
        images[module_id] = f"{repository}@{digest}"
        modules[module_id] = version

    for module_id, (image_env, revision_env) in EXTERNAL_MODULE_INPUTS.items():
        image_ref = _required(values, image_env)
        image_error = validate_image_ref(image_ref)
        if image_error:
            raise ValueError(f"{image_env} is invalid: {image_error}")
        revision = _required(values, revision_env)
        images[module_id] = image_ref
        modules[module_id] = revision

    rollback_kind = (values.get("ONEBRAIN_ROLLBACK_KIND") or "").strip()
    if not rollback_kind:
        rollback_kind = classify_current_migration_delta(values)
    migration_to = (values.get("ONEBRAIN_MIGRATION_TO") or "").strip() or current_alembic_head()
    common = {
        "version": version,
        "git_sha": git_sha,
        "modules": modules,
        "images": images,
        "migration_from": (values.get("ONEBRAIN_MIGRATION_FROM") or "").strip(),
        "migration_to": migration_to,
        "rollback_kind": rollback_kind,
        "security_notes": "",
        "rollback_plan": "",
    }
    endpoint = f"{base_url}/api/operator/release-candidates"
    prepared = _post_json(
        endpoint,
        {"action": "prepare", **common},
        key_id=key_id,
        secret=secret,
        opener=opener,
    )
    release_data = prepared.get("release") or {}
    release = ReleaseManifest(
        version=str(release_data.get("version", "")),
        git_sha=str(release_data.get("git_sha", "")),
        modules=dict(release_data.get("modules") or {}),
        migration_from=str(release_data.get("migration_from", "")),
        migration_to=str(release_data.get("migration_to", "")),
        rollback_kind=str(release_data.get("rollback_kind", "")),
        images=dict(release_data.get("images") or {}),
    )
    dev_signature = sign_release(release_signature_fields(release), dev_private_key)
    registered = _post_json(
        endpoint,
        {
            "action": "register",
            **release_signature_fields(release),
            "security_notes": "",
            "rollback_plan": "",
            "dev_signature": dev_signature,
            "dev_signing_key_id": (values.get("ONEBRAIN_DEV_RELEASE_KEY_ID") or "dev-ci-v1").strip(),
        },
        key_id=key_id,
        secret=secret,
        opener=opener,
    )
    return registered


def main() -> int:
    try:
        result = register_from_environment()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    promotion = ((result.get("release") or {}).get("promotion") or {}).get("state", "registered")
    print(f"release candidate {result.get('manifest_digest', '')} is {promotion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

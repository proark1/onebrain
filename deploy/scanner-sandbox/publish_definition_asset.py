#!/usr/bin/env python3
"""Publish a validated ClamAV baseline as a GitHub immutable release.

This command intentionally has no overwrite, delete, or clobber path. It can
idempotently re-verify an exact immutable publication after a partial failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import worker_supply_chain as supply_chain


_REPOSITORY = "proark1/onebrain"
_API_VERSION = "2026-03-10"
_MAIN_REF = "refs/heads/main"
_PUBLICATION_WORKFLOW = ".github/workflows/publish-scanner-definitions.yml"
_SUPPLY_CHAIN_LOCK = "deploy/scanner-sandbox/worker-supply-chain.lock.json"
_GENERATOR_FILES = (
    _PUBLICATION_WORKFLOW,
    "Dockerfile.worker",
    "deploy/scanner-sandbox/worker_supply_chain.py",
    "deploy/scanner-sandbox/publish_definition_asset.py",
    "deploy/scanner-sandbox/freshclam.conf",
)
_FRAGMENT_FIELDS = {
    "url",
    "sha256",
    "identity",
    "archive_manifest_sha256",
    "max_bytes",
}


class PublicationError(RuntimeError):
    """A content-free immutable publication error."""


@dataclass(frozen=True)
class PublicationInput:
    artifact: Path
    tag: str
    identity: str
    sha256: str
    manifest_sha256: str
    size: int


def validate_input(artifact: Path, fragment_file: Path) -> PublicationInput:
    fragment = supply_chain._load_json(
        fragment_file,
        code="definition_publication_fragment_invalid",
    )
    if set(fragment) != _FRAGMENT_FIELDS:
        raise PublicationError("definition_publication_fragment_invalid")
    identity = str(fragment.get("identity") or "")
    if not supply_chain._IDENTITY_RE.fullmatch(identity):
        raise PublicationError("definition_publication_fragment_invalid")
    expected_name = f"onebrain-clamav-definitions-{identity}.tar.gz"
    if artifact.name != expected_name or not artifact.is_file():
        raise PublicationError("definition_publication_artifact_invalid")
    try:
        expected_url = supply_chain._definition_url(fragment["url"], identity=identity)
        sha256 = supply_chain._digest(
            fragment["sha256"], code="definition_publication_fragment_invalid"
        )
        manifest_sha256 = supply_chain._digest(
            fragment["archive_manifest_sha256"],
            code="definition_publication_fragment_invalid",
        )
    except supply_chain.SupplyChainError as exc:
        raise PublicationError(str(exc)) from exc
    tag = f"scanner-definitions-{identity}"
    if not expected_url.endswith(f"/{tag}/{expected_name}"):
        raise PublicationError("definition_publication_url_mismatch")
    maximum = fragment.get("max_bytes")
    size = artifact.stat().st_size
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum != supply_chain._MAX_ARTIFACT_BYTES
        or size <= 0
        or size > maximum
        or supply_chain._sha256_file(artifact) != sha256
    ):
        raise PublicationError("definition_publication_artifact_invalid")
    with tempfile.TemporaryDirectory(prefix="onebrain-publication-verify-") as temporary:
        destination = Path(temporary) / "definitions"
        destination.mkdir()
        try:
            supply_chain._extract_definition_archive(
                artifact,
                destination,
                identity=identity,
                expected_manifest_sha256=manifest_sha256,
            )
        except supply_chain.SupplyChainError as exc:
            raise PublicationError(str(exc)) from exc
    return PublicationInput(
        artifact=artifact.resolve(),
        tag=tag,
        identity=identity,
        sha256=sha256,
        manifest_sha256=manifest_sha256,
        size=size,
    )


def publish(
    value: PublicationInput,
    *,
    supply_chain_lock: Path,
    target: str,
    confirm_tag: str,
) -> None:
    if confirm_tag != value.tag:
        raise PublicationError("definition_publication_confirmation_mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise PublicationError("definition_publication_target_invalid")
    if shutil.which("gh") is None:
        raise PublicationError("definition_publication_gh_missing")
    _require_gh_attestation_support()

    immutable = _api_json(
        "GET",
        f"repos/{_REPOSITORY}/immutable-releases",
        code="definition_publication_immutability_unavailable",
    )
    if immutable.get("enabled") is not True:
        raise PublicationError("definition_publication_immutability_disabled")
    generator_inputs_sha256 = _verify_protected_generator_inputs(
        supply_chain_lock,
        target=target,
    )
    notes = _release_notes(
        value,
        target=target,
        generator_inputs_sha256=generator_inputs_sha256,
    )
    existing_release_id = _existing_release_id(value)
    if existing_release_id is not None:
        _verify_existing_release(
            value,
            release_id=existing_release_id,
            target=target,
            expected_body=notes,
        )
        return

    draft = _api_json(
        "POST",
        f"repos/{_REPOSITORY}/releases",
        code="definition_publication_draft_failed",
        payload={
            "tag_name": value.tag,
            "target_commitish": target,
            "name": value.tag,
            "body": notes,
            "draft": True,
            "prerelease": False,
            "make_latest": "false",
        },
    )
    release_id = _validate_created_draft(
        draft,
        value=value,
        target=target,
        expected_body=notes,
    )
    upload_url = _asset_upload_url(
        draft.get("upload_url"),
        release_id=release_id,
        asset_name=value.artifact.name,
    )
    uploaded_asset = _api_json(
        "POST",
        upload_url,
        code="definition_publication_asset_upload_failed",
        input_file=value.artifact,
        headers=("Content-Type: application/gzip",),
    )
    asset_id = _validate_asset(uploaded_asset, value=value)
    _wait_for_release_id(
        value,
        release_id=release_id,
        asset_id=asset_id,
        target=target,
        expected_body=notes,
        draft=True,
        require_immutable=False,
    )

    patched = _api_json(
        "PATCH",
        f"repos/{_REPOSITORY}/releases/{release_id}",
        code="definition_publication_publish_failed",
        payload={"draft": False, "make_latest": "false"},
    )
    _validate_release(
        patched,
        value=value,
        release_id=release_id,
        asset_id=asset_id,
        target=target,
        expected_body=notes,
        draft=False,
        require_immutable=None,
    )
    _wait_for_release_id(
        value,
        release_id=release_id,
        asset_id=asset_id,
        target=target,
        expected_body=notes,
        draft=False,
        require_immutable=True,
    )

    _verify_tag_and_attestations(value, target=target)


def _verify_existing_release(
    value: PublicationInput,
    *,
    release_id: int,
    target: str,
    expected_body: str,
) -> None:
    release = _api_json(
        "GET",
        f"repos/{_REPOSITORY}/releases/{release_id}",
        code="definition_publication_release_unavailable",
    )
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 1:
        raise PublicationError("definition_publication_asset_invalid")
    asset_id = _validate_asset(assets[0], value=value)
    _validate_release(
        release,
        value=value,
        release_id=release_id,
        asset_id=asset_id,
        target=target,
        expected_body=expected_body,
        draft=False,
        require_immutable=True,
    )
    _verify_tag_and_attestations(value, target=target)


def _release_notes(
    value: PublicationInput,
    *,
    target: str,
    generator_inputs_sha256: str,
) -> str:
    return (
        "Immutable OneBrain ClamAV image baseline.\n\n"
        f"Source commit: `{target}`\n"
        f"Generator inputs SHA-256: `{generator_inputs_sha256}`\n"
        f"Artifact SHA-256: `{value.sha256}`\n"
        f"Canonical manifest SHA-256: `{value.manifest_sha256}`\n"
    )


def _verify_tag_and_attestations(value: PublicationInput, *, target: str) -> None:
    tag_reference = _api_json(
        "GET",
        f"repos/{_REPOSITORY}/git/ref/tags/{value.tag}",
        code="definition_publication_tag_unavailable",
    )
    tag_object = tag_reference.get("object")
    if (
        not isinstance(tag_object, dict)
        or tag_object.get("type") != "commit"
        or tag_object.get("sha") != target
    ):
        raise PublicationError("definition_publication_tag_target_mismatch")

    _retry_attestation(
        ["gh", "release", "verify", value.tag, "--repo", _REPOSITORY],
        code="definition_publication_release_attestation_failed",
    )
    _retry_attestation(
        [
            "gh",
            "release",
            "verify-asset",
            value.tag,
            str(value.artifact),
            "--repo",
            _REPOSITORY,
        ],
        code="definition_publication_asset_attestation_failed",
    )


def _verify_protected_generator_inputs(
    supply_chain_lock: Path,
    *,
    target: str,
) -> str:
    _require_protected_workflow_context(target=target)
    branch = _api_json(
        "GET",
        f"repos/{_REPOSITORY}/branches/main",
        code="definition_publication_target_unavailable",
    )
    branch_commit = branch.get("commit")
    if (
        branch.get("name") != "main"
        or branch.get("protected") is not True
        or not isinstance(branch_commit, dict)
        or branch_commit.get("sha") != target
    ):
        raise PublicationError("definition_publication_target_mismatch")

    repository_root = Path(__file__).resolve().parents[2]
    expected_lock_path = (repository_root / _SUPPLY_CHAIN_LOCK).resolve()
    try:
        actual_lock_path = supply_chain_lock.resolve(strict=True)
    except OSError as exc:
        raise PublicationError("definition_publication_local_input_invalid") from exc
    if actual_lock_path != expected_lock_path:
        raise PublicationError("definition_publication_local_input_invalid")

    local_files: dict[str, bytes] = {}
    for relative_path in _GENERATOR_FILES:
        try:
            local_bytes = (repository_root / relative_path).read_bytes()
        except OSError as exc:
            raise PublicationError("definition_publication_local_input_invalid") from exc
        remote_bytes = _remote_file_bytes(relative_path, target=target)
        if remote_bytes != local_bytes:
            raise PublicationError("definition_publication_generator_input_mismatch")
        local_files[relative_path] = local_bytes

    with tempfile.TemporaryDirectory(prefix="onebrain-target-lock-") as temporary:
        remote_lock_path = Path(temporary) / "worker-supply-chain.lock.json"
        remote_lock_path.write_bytes(_remote_file_bytes(_SUPPLY_CHAIN_LOCK, target=target))
        try:
            local_lock = supply_chain.load_lock(actual_lock_path)
            remote_lock = supply_chain.load_lock(remote_lock_path)
        except supply_chain.SupplyChainError as exc:
            raise PublicationError("definition_publication_target_lock_invalid") from exc

    local_projection = _generator_lock_projection(local_lock)
    remote_projection = _generator_lock_projection(remote_lock)
    if local_projection != remote_projection:
        raise PublicationError("definition_publication_target_lock_mismatch")

    evidence = hashlib.sha256()
    for relative_path in _GENERATOR_FILES:
        _extend_evidence(evidence, relative_path, local_files[relative_path])
    _extend_evidence(evidence, _SUPPLY_CHAIN_LOCK, local_projection)
    return evidence.hexdigest()


def _require_protected_workflow_context(*, target: str) -> None:
    expected_workflow_ref = f"{_REPOSITORY}/{_PUBLICATION_WORKFLOW}@{_MAIN_REF}"
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": _REPOSITORY,
        "GITHUB_REF": _MAIN_REF,
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": target,
        "GITHUB_WORKFLOW_REF": expected_workflow_ref,
    }
    if any(os.environ.get(name) != expected for name, expected in required.items()):
        raise PublicationError("definition_publication_context_invalid")


def _remote_file_bytes(relative_path: str, *, target: str) -> bytes:
    quoted_path = urllib.parse.quote(relative_path, safe="/")
    content = _api_json(
        "GET",
        f"repos/{_REPOSITORY}/contents/{quoted_path}?ref={target}",
        code="definition_publication_target_input_unavailable",
    )
    encoded_content = content.get("content")
    if (
        content.get("type") != "file"
        or content.get("path") != relative_path
        or content.get("encoding") != "base64"
        or not isinstance(encoded_content, str)
    ):
        raise PublicationError("definition_publication_target_input_invalid")
    try:
        return base64.b64decode("".join(encoded_content.split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise PublicationError("definition_publication_target_input_invalid") from exc


def _generator_lock_projection(lock: supply_chain.WorkerSupplyChainLock) -> bytes:
    value = {
        "schema": 1,
        "base_image": {
            "reference": lock.base_reference,
            "linux_amd64_manifest_digest": lock.linux_amd64_manifest_digest,
        },
        "debian": {
            "suite": lock.suite,
            "architecture": lock.architecture,
            "keyring": lock.keyring,
            "snapshots": {
                name: {
                    "url": snapshot.url,
                    "suite": snapshot.suite,
                    "components": list(snapshot.components),
                }
                for name, snapshot in sorted(lock.snapshots.items())
            },
        },
        "stages": {
            name: {
                "packages": [
                    {"name": package.name, "version": package.version}
                    for package in stage.packages
                ],
                "inventory_sha256": stage.inventory_sha256,
            }
            for name, stage in sorted(lock.stages.items())
        },
    }
    return supply_chain._canonical_json(value)


def _extend_evidence(digest: "hashlib._Hash", name: str, value: bytes) -> None:
    encoded_name = name.encode("utf-8")
    digest.update(len(encoded_name).to_bytes(4, "big"))
    digest.update(encoded_name)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _retry_attestation(command: Sequence[str], *, code: str) -> None:
    for attempt in range(10):
        if _run_capture(command).returncode == 0:
            return
        if attempt < 9:
            time.sleep(2)
    raise PublicationError(code)


def _require_gh_attestation_support() -> None:
    commands = (
        ("gh", "release", "verify", "--help"),
        ("gh", "release", "verify-asset", "--help"),
    )
    if any(_run_capture(command).returncode != 0 for command in commands):
        raise PublicationError("definition_publication_gh_attestation_unsupported")


def _existing_release_id(value: PublicationInput) -> int | None:
    releases = _list_releases()
    matches: list[dict] = []
    for release in releases:
        if not isinstance(release, dict):
            raise PublicationError("definition_publication_release_list_invalid")
        assets = release.get("assets")
        if assets is not None and not isinstance(assets, list):
            raise PublicationError("definition_publication_release_list_invalid")
        if (
            release.get("tag_name") == value.tag
            or release.get("name") == value.tag
            or any(
                isinstance(asset, dict) and asset.get("name") == value.artifact.name
                for asset in assets or []
            )
        ):
            matches.append(release)
    if matches:
        if len(matches) != 1:
            raise PublicationError("definition_publication_release_exists")
        release = matches[0]
        release_id = release.get("id")
        if (
            not isinstance(release_id, int)
            or isinstance(release_id, bool)
            or release_id <= 0
            or release.get("tag_name") != value.tag
            or release.get("name") != value.tag
            or release.get("draft") is not False
            or release.get("prerelease") is not False
        ):
            raise PublicationError("definition_publication_release_exists")
        return release_id
    _require_absent(
        f"repos/{_REPOSITORY}/git/ref/tags/{value.tag}",
        code="definition_publication_tag_exists",
    )
    return None


def _list_releases() -> list[dict]:
    command = [
        "gh",
        "api",
        "--method",
        "GET",
        "--paginate",
        "--slurp",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {_API_VERSION}",
        f"repos/{_REPOSITORY}/releases?per_page=100",
    ]
    value = _run_json_value(command, code="definition_publication_release_list_failed")
    if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
        raise PublicationError("definition_publication_release_list_invalid")
    return [release for page in value for release in page]


def _validate_created_draft(
    release: dict,
    *,
    value: PublicationInput,
    target: str,
    expected_body: str,
) -> int:
    release_id = release.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise PublicationError("definition_publication_draft_invalid")
    _validate_release_identity(
        release,
        value=value,
        release_id=release_id,
        target=target,
        expected_body=expected_body,
        draft=True,
    )
    if release.get("assets") != [] or release.get("immutable") not in {False, None}:
        raise PublicationError("definition_publication_draft_invalid")
    return release_id


def _asset_upload_url(raw_url: object, *, release_id: int, asset_name: str) -> str:
    if not isinstance(raw_url, str):
        raise PublicationError("definition_publication_upload_url_invalid")
    url = raw_url.split("{", 1)[0]
    parsed = urllib.parse.urlsplit(url)
    expected_path = f"/repos/{_REPOSITORY}/releases/{release_id}/assets"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "uploads.github.com"
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise PublicationError("definition_publication_upload_url_invalid")
    return f"{url}?{urllib.parse.urlencode({'name': asset_name})}"


def _validate_asset(asset: dict, *, value: PublicationInput) -> int:
    asset_id = asset.get("id")
    expected_url = (
        f"https://github.com/{_REPOSITORY}/releases/download/"
        f"{value.tag}/{value.artifact.name}"
    )
    if (
        not isinstance(asset_id, int)
        or isinstance(asset_id, bool)
        or asset_id <= 0
        or asset.get("name") != value.artifact.name
        or asset.get("size") != value.size
        or asset.get("digest") != f"sha256:{value.sha256}"
        or asset.get("state") != "uploaded"
        or asset.get("browser_download_url") != expected_url
    ):
        raise PublicationError("definition_publication_asset_invalid")
    return asset_id


def _validate_release_identity(
    release: dict,
    *,
    value: PublicationInput,
    release_id: int,
    target: str,
    expected_body: str,
    draft: bool,
) -> None:
    if (
        release.get("id") != release_id
        or release.get("tag_name") != value.tag
        or release.get("target_commitish") != target
        or release.get("name") != value.tag
        or release.get("body") != expected_body
        or release.get("draft") is not draft
        or release.get("prerelease") is not False
    ):
        raise PublicationError("definition_publication_release_identity_mismatch")


def _validate_release(
    release: dict,
    *,
    value: PublicationInput,
    release_id: int,
    asset_id: int,
    target: str,
    expected_body: str,
    draft: bool,
    require_immutable: bool | None,
) -> None:
    _validate_release_identity(
        release,
        value=value,
        release_id=release_id,
        target=target,
        expected_body=expected_body,
        draft=draft,
    )
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 1:
        raise PublicationError("definition_publication_asset_invalid")
    verified_asset_id = _validate_asset(assets[0], value=value)
    if verified_asset_id != asset_id:
        raise PublicationError("definition_publication_asset_identity_mismatch")
    immutable = release.get("immutable")
    if require_immutable is None:
        if immutable is not False and immutable is not True:
            raise PublicationError("definition_publication_immutable_state_mismatch")
    elif draft and require_immutable is False:
        if immutable is not False and immutable is not None:
            raise PublicationError("definition_publication_immutable_state_mismatch")
    elif immutable is not require_immutable:
        raise PublicationError("definition_publication_immutable_state_mismatch")


def _wait_for_release_id(
    value: PublicationInput,
    *,
    release_id: int,
    asset_id: int,
    target: str,
    expected_body: str,
    draft: bool,
    require_immutable: bool,
) -> dict:
    for attempt in range(10):
        release = _api_json(
            "GET",
            f"repos/{_REPOSITORY}/releases/{release_id}",
            code="definition_publication_release_unavailable",
        )
        try:
            _validate_release(
                release,
                value=value,
                release_id=release_id,
                asset_id=asset_id,
                target=target,
                expected_body=expected_body,
                draft=draft,
                require_immutable=require_immutable,
            )
        except PublicationError as exc:
            if (
                str(exc) == "definition_publication_immutable_state_mismatch"
                and require_immutable
                and attempt < 9
            ):
                time.sleep(2)
                continue
            raise
        else:
            return release
    raise PublicationError("definition_publication_release_invalid")


def _require_absent(endpoint: str, *, code: str) -> None:
    completed = _run_capture(_api_command("GET", endpoint))
    combined = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0:
        raise PublicationError(code)
    if "HTTP 404" not in combined:
        raise PublicationError("definition_publication_preflight_failed")


def _api_command(method: str, endpoint: str) -> list[str]:
    return [
        "gh",
        "api",
        "--method",
        method,
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {_API_VERSION}",
        endpoint,
    ]


def _api_json(
    method: str,
    endpoint: str,
    *,
    code: str,
    payload: dict | None = None,
    input_file: Path | None = None,
    headers: Sequence[str] = (),
) -> dict:
    if payload is not None and input_file is not None:
        raise PublicationError("definition_publication_request_invalid")
    command = _api_command(method, endpoint)
    endpoint_value = command.pop()
    for header in headers:
        command.extend(("-H", header))
    input_text: str | None = None
    if payload is not None:
        command.extend(("--input", "-"))
        input_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    elif input_file is not None:
        command.extend(("--input", str(input_file)))
    command.append(endpoint_value)
    completed = _run_capture(command, input_text=input_text)
    if completed.returncode != 0:
        raise PublicationError(code)
    try:
        value = json.loads(completed.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationError(code) from exc
    if not isinstance(value, dict):
        raise PublicationError(code)
    return value


def _run_json_value(command: Sequence[str], *, code: str) -> object:
    completed = _run_capture(command)
    if completed.returncode != 0:
        raise PublicationError(code)
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationError(code) from exc


def _run_capture(
    command: Sequence[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5 * 60,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError("definition_publication_command_failed") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("fragment", type=Path)
    parser.add_argument("--supply-chain-lock", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--confirm-tag", required=True)
    args = parser.parse_args(argv)
    value = validate_input(args.artifact, args.fragment)
    publish(
        value,
        supply_chain_lock=args.supply_chain_lock,
        target=args.target,
        confirm_tag=args.confirm_tag,
    )
    print(value.tag)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

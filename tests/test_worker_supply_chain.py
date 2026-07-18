from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "scanner-sandbox" / "worker_supply_chain.py"
_LOCK = _ROOT / "deploy" / "scanner-sandbox" / "worker-supply-chain.lock.json"
_SPEC = importlib.util.spec_from_file_location("worker_supply_chain", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
supply_chain = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = supply_chain
_SPEC.loader.exec_module(supply_chain)
_PUBLISH_SCRIPT = (
    _ROOT / "deploy" / "scanner-sandbox" / "publish_definition_asset.py"
)
_PUBLISH_SPEC = importlib.util.spec_from_file_location(
    "publish_definition_asset", _PUBLISH_SCRIPT
)
assert _PUBLISH_SPEC is not None and _PUBLISH_SPEC.loader is not None
publication = importlib.util.module_from_spec(_PUBLISH_SPEC)
sys.modules[_PUBLISH_SPEC.name] = publication
_PUBLISH_SPEC.loader.exec_module(publication)


def _lock_value() -> dict:
    return json.loads(_LOCK.read_text(encoding="utf-8"))


def _write_lock(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _archive_fixture(tmp_path: Path):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    paths = []
    for name, value in (
        ("main.cvd", b"main definitions"),
        ("daily.cvd", b"daily definitions"),
        ("bytecode.cvd", b"bytecode definitions"),
    ):
        path = definitions / name
        path.write_bytes(value)
        paths.append(path)
    files = {
        path.name: {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in paths
    }
    manifest = {
        "schema": 1,
        "identity": "clamav-fixture",
        "files": files,
    }
    manifest_bytes = supply_chain._canonical_json(manifest)
    archive = tmp_path / "definitions.tar.gz"
    supply_chain._write_deterministic_archive(
        archive,
        database_paths=paths,
        manifest_bytes=manifest_bytes,
    )
    return archive, manifest_bytes, paths


def test_checked_in_lock_and_dockerfile_form_one_strict_contract():
    lock = supply_chain.load_lock(_LOCK)

    supply_chain.verify_dockerfile(lock, _ROOT / "Dockerfile.worker")

    assert lock.base_reference.endswith(
        "@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    )
    assert lock.architecture == "amd64"
    assert lock.snapshots["debian"].url == (
        "https://snapshot.debian.org/archive/debian/20260714T000000Z/"
    )
    assert lock.stages["worker-runtime"].inventory_sha256 == (
        "8f5527a21e5569e8c214c064434fd14d47a43cb823928dcff51c4ad3239f555a"
    )
    assert [package.apt_spec for package in lock.stages["worker-runtime"].packages] == [
        "ca-certificates=20250419",
        "clamav=1.4.3+dfsg-1",
        "clamav-freshclam=1.4.3+dfsg-1",
        "libseccomp2=2.6.0-2",
        "tesseract-ocr=5.5.0-1+b1",
    ]


def test_dockerfile_has_no_live_apt_or_production_definition_download_path():
    dockerfile = (_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    assert "apt-get" not in dockerfile
    assert "deb.debian.org" not in dockerfile
    assert "security.debian.org" not in dockerfile
    assert "allow-unauthenticated" not in dockerfile.lower()
    assert "trusted=yes" not in dockerfile.lower()
    assert "FROM scanner-definition-artifact-bootstrap" not in dockerfile
    assert dockerfile.count("bootstrap-definitions") == 1
    bootstrap = dockerfile.split(
        "FROM worker-base AS scanner-definition-artifact-build", 1
    )[1].split("FROM scratch AS scanner-definition-artifact-bootstrap", 1)[0]
    assert "useradd --system --uid 10001" in bootstrap
    assert "install -d --owner=onebrain --group=onebrain" in bootstrap
    assert "USER onebrain" in bootstrap
    assert "TMPDIR=/tmp/onebrain-definition-bootstrap" in bootstrap
    export = dockerfile.split(
        "FROM scratch AS scanner-definition-artifact-bootstrap", 1
    )[1].split("FROM worker-base AS worker-runtime", 1)[0]
    assert "COPY --from=scanner-definition-artifact-build /out/ /" in export
    assert "COPY --from=scanner-definition-artifact-build / " not in export
    assert "fetch-definitions" in dockerfile
    assert "--supply-chain /opt/onebrain/worker-supply-chain.json" in dockerfile


def test_dockerfile_rejects_external_base_that_self_aliases(tmp_path: Path):
    dockerfile = (_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    mutated = tmp_path / "Dockerfile.worker"
    mutated.write_text(f"FROM evil.example/base AS evil\n{dockerfile}", encoding="utf-8")

    with pytest.raises(
        supply_chain.SupplyChainError,
        match="supply_chain_dockerfile_external_base",
    ):
        supply_chain.verify_dockerfile(supply_chain.load_lock(_LOCK), mutated)


@pytest.mark.parametrize(
    ("instruction", "error"),
    [
        (
            "ADD https://example.invalid/unlocked /tmp/unlocked",
            "supply_chain_dockerfile_add_forbidden",
        ),
        (
            "RUN --mount=from=scanner-runtime-refresh-validation,target=/mnt true",
            "supply_chain_dockerfile_run_mount_forbidden",
        ),
        (
            "ADD \\\n    https://example.invalid/multiline /tmp/unlocked",
            "supply_chain_dockerfile_add_forbidden",
        ),
        (
            "RUN \\\n    --mount=from=scanner-runtime-refresh-validation,target=/mnt true",
            "supply_chain_dockerfile_run_mount_forbidden",
        ),
    ],
)
def test_dockerfile_rejects_hidden_buildkit_dependency_paths(
    tmp_path: Path,
    instruction: str,
    error: str,
):
    dockerfile = (_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    mutated = tmp_path / "Dockerfile.worker"
    mutated.write_text(
        dockerfile.replace("\nWORKDIR /app", f"\n{instruction}\n\nWORKDIR /app", 1),
        encoding="utf-8",
    )

    with pytest.raises(supply_chain.SupplyChainError, match=error):
        supply_chain.verify_dockerfile(supply_chain.load_lock(_LOCK), mutated)


def test_final_image_graph_excludes_networked_definition_refresh(tmp_path: Path):
    dockerfile = (_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    baseline_smoke = (
        _ROOT / "deploy" / "scanner-sandbox" / "image-smoke.sh"
    ).read_text(encoding="utf-8")
    refresh_smoke = (
        _ROOT / "deploy" / "scanner-sandbox" / "runtime-refresh-smoke.sh"
    ).read_text(encoding="utf-8")

    assert "definitions-update" not in baseline_smoke
    assert "freshclam" not in baseline_smoke.lower()
    assert '"${launcher}" definitions-update' in refresh_smoke
    assert "FROM worker-runtime AS scanner-runtime-refresh-validation" in dockerfile
    final = dockerfile.split("FROM worker-runtime AS final", 1)[1]
    assert "scanner-runtime-refresh-validation" not in final

    mutated = tmp_path / "Dockerfile.worker"
    mutated.write_text(
        dockerfile.replace(
            "COPY --from=scanner-validation --chown=root:root \\",
            "COPY \\\n    --from=scanner-runtime-refresh-validation --chown=root:root \\",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        supply_chain.SupplyChainError,
        match="supply_chain_network_stage_in_production_graph",
    ):
        supply_chain.verify_dockerfile(supply_chain.load_lock(_LOCK), mutated)


@pytest.mark.parametrize(
    "command",
    ["/usr/bin/freshclam --datadir=/tmp/definitions", "definitions-update /tmp/set"],
)
def test_final_graph_rejects_network_commands_hidden_in_run_heredoc(
    tmp_path: Path,
    command: str,
):
    dockerfile = (_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    mutated = tmp_path / "Dockerfile.worker"
    heredoc = f"RUN <<'ONEBRAIN_EOF'\n{command}\nONEBRAIN_EOF"
    mutated.write_text(
        dockerfile.replace("\nWORKDIR /app", f"\n{heredoc}\n\nWORKDIR /app", 1),
        encoding="utf-8",
    )

    with pytest.raises(
        supply_chain.SupplyChainError,
        match="supply_chain_network_command_in_production_graph",
    ):
        supply_chain.verify_dockerfile(supply_chain.load_lock(_LOCK), mutated)


def test_base_index_digest_and_linux_amd64_child_are_both_enforced(tmp_path: Path):
    child = f"sha256:{'2' * 64}"
    raw_index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": child,
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
                {
                    "digest": f"sha256:{'3' * 64}",
                    "platform": {"architecture": "arm64", "os": "linux"},
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    value = _lock_value()
    value["base_image"]["reference"] = (
        "python:3.12-slim@sha256:" + hashlib.sha256(raw_index).hexdigest()
    )
    value["base_image"]["linux_amd64_manifest_digest"] = child
    lock = supply_chain.load_lock(_write_lock(tmp_path, value))

    assert supply_chain.verify_base_image_index(lock, raw_index + b"\n") == child
    with pytest.raises(
        supply_chain.SupplyChainError,
        match="supply_chain_base_index_digest_mismatch",
    ):
        supply_chain.verify_base_image_index(lock, raw_index + b" ")

    mismatch = copy.deepcopy(value)
    mismatch["base_image"]["linux_amd64_manifest_digest"] = f"sha256:{'4' * 64}"
    mismatched_lock = supply_chain.load_lock(_write_lock(tmp_path, mismatch))
    with pytest.raises(
        supply_chain.SupplyChainError,
        match="supply_chain_base_manifest_digest_mismatch",
    ):
        supply_chain.verify_base_image_index(mismatched_lock, raw_index)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value["base_image"].update({"unknown": True}),
        lambda value: value["stages"]["worker-runtime"]["packages"].append(
            copy.deepcopy(value["stages"]["worker-runtime"]["packages"][0])
        ),
        lambda value: value["stages"]["worker-runtime"]["packages"][0].update(
            {"version": ""}
        ),
        lambda value: value["stages"]["worker-runtime"].update(
            {"inventory_sha256": "not-a-digest"}
        ),
        lambda value: value["debian"]["snapshots"]["debian"].update(
            {"url": "https://deb.debian.org/debian"}
        ),
        lambda value: value["definitions"].update(
            {"url": "http://github.com/proark1/onebrain/artifact.tar.gz"}
        ),
        lambda value: value["definitions"].update({"sha256": "0" * 63}),
        lambda value: value["stages"].pop("scanner-validation"),
    ],
)
def test_lock_parser_rejects_drift_and_ambiguous_inputs(tmp_path: Path, mutation):
    value = _lock_value()
    mutation(value)

    with pytest.raises(supply_chain.SupplyChainError):
        supply_chain.load_lock(_write_lock(tmp_path, value))


def test_definition_archive_is_deterministic_and_extracts_only_verified_members(
    tmp_path: Path,
):
    archive, manifest_bytes, paths = _archive_fixture(tmp_path)
    second = tmp_path / "second.tar.gz"
    supply_chain._write_deterministic_archive(
        second,
        database_paths=paths,
        manifest_bytes=manifest_bytes,
    )
    destination = tmp_path / "extracted"
    destination.mkdir()

    supply_chain._extract_definition_archive(
        archive,
        destination,
        identity="clamav-fixture",
        expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )

    assert archive.read_bytes() == second.read_bytes()
    assert {path.name for path in destination.iterdir()} == {
        "main.cvd",
        "daily.cvd",
        "bytecode.cvd",
        "manifest.json",
    }
    assert (destination / "daily.cvd").read_bytes() == b"daily definitions"


def _malicious_archive(tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "malicious.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        for info, value in members:
            info.size = len(value)
            bundle.addfile(info, io.BytesIO(value))
    return path


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo("../daily.cvd"),
        tarfile.TarInfo("/daily.cvd"),
        tarfile.TarInfo("nested/daily.cvd"),
        tarfile.TarInfo("unexpected.txt"),
    ],
)
def test_definition_archive_rejects_traversal_and_unexpected_members(
    tmp_path: Path, member: tarfile.TarInfo
):
    archive, manifest_bytes, _paths = _archive_fixture(tmp_path)
    with tarfile.open(archive, "r:gz") as source:
        members = []
        for original in source.getmembers():
            stream = source.extractfile(original)
            assert stream is not None
            members.append((original, stream.read()))
    members.append((member, b"malicious"))
    malicious = _malicious_archive(tmp_path, members)
    destination = tmp_path / "out"
    destination.mkdir()

    with pytest.raises(supply_chain.SupplyChainError):
        supply_chain._extract_definition_archive(
            malicious,
            destination,
            identity="clamav-fixture",
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )


def test_definition_archive_rejects_links_duplicate_members_and_manifest_drift(
    tmp_path: Path,
):
    archive, manifest_bytes, _paths = _archive_fixture(tmp_path)
    with tarfile.open(archive, "r:gz") as source:
        members = []
        for original in source.getmembers():
            stream = source.extractfile(original)
            assert stream is not None
            members.append((original, stream.read()))
    link = tarfile.TarInfo("linked.cvd")
    link.type = tarfile.SYMTYPE
    link.linkname = "daily.cvd"
    cases = (
        members + [(copy.copy(members[1][0]), members[1][1])],
        members + [(link, b"")],
    )
    for number, case in enumerate(cases):
        malicious = _malicious_archive(tmp_path / f"case-{number}", case)
        destination = tmp_path / f"out-{number}"
        destination.mkdir()
        with pytest.raises(supply_chain.SupplyChainError):
            supply_chain._extract_definition_archive(
                malicious,
                destination,
                identity="clamav-fixture",
                expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )

    destination = tmp_path / "manifest-out"
    destination.mkdir()
    with pytest.raises(
        supply_chain.SupplyChainError,
        match="definition_artifact_manifest_checksum_mismatch",
    ):
        supply_chain._extract_definition_archive(
            archive,
            destination,
            identity="clamav-fixture",
            expected_manifest_sha256="0" * 64,
        )


def test_definition_extractor_streams_with_member_and_expanded_size_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _SCRIPT.read_text(encoding="utf-8")
    assert 'tarfile.open(archive, mode="r|gz")' in source
    assert ".getmembers()" not in source
    assert "_MAX_ARCHIVE_MEMBERS = 4" in source

    archive, manifest_bytes, _paths = _archive_fixture(tmp_path)
    destination = tmp_path / "bounded"
    destination.mkdir()
    monkeypatch.setattr(supply_chain, "_MAX_EXPANDED_DEFINITION_BYTES", 16)

    with pytest.raises(
        supply_chain.SupplyChainError,
        match="definition_artifact_expanded_size_limit",
    ):
        supply_chain._extract_definition_archive(
            archive,
            destination,
            identity="clamav-fixture",
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )

    assert list(destination.iterdir()) == []


def test_supply_chain_evidence_is_minimal_and_binds_all_resolution_authorities():
    lock = supply_chain.load_lock(_LOCK)

    evidence = supply_chain.supply_chain_evidence(lock, stage_name="worker-runtime")

    assert evidence["lock_sha256"] == hashlib.sha256(_LOCK.read_bytes()).hexdigest()
    assert evidence["inventory_sha256"] == lock.stages["worker-runtime"].inventory_sha256
    assert evidence["definition_artifact"]["sha256"] == lock.definitions.sha256
    assert evidence["snapshots"]["debian-security"]["suite"] == "trixie-security"
    assert "updated_at" not in evidence


def _publication_value() -> publication.PublicationInput:
    lock = supply_chain.load_lock(_LOCK)
    return publication.PublicationInput(
        artifact=Path(
            "onebrain-clamav-definitions-"
            f"{lock.definitions.identity}.tar.gz"
        ),
        tag=f"scanner-definitions-{lock.definitions.identity}",
        identity=lock.definitions.identity,
        sha256=lock.definitions.sha256,
        manifest_sha256=lock.definitions.archive_manifest_sha256,
        size=123,
    )


def _publication_asset(
    value: publication.PublicationInput,
    *,
    asset_id: int = 7,
    draft: bool = False,
):
    release_name = "untagged-deadbeef" if draft else value.tag
    return {
        "id": asset_id,
        "name": value.artifact.name,
        "size": value.size,
        "digest": f"sha256:{value.sha256}",
        "state": "uploaded",
        "browser_download_url": (
            "https://github.com/proark1/onebrain/releases/download/"
            f"{release_name}/{value.artifact.name}"
        ),
    }


def _publication_release(
    value: publication.PublicationInput,
    *,
    target: str,
    draft: bool,
    immutable: bool | None,
    assets: list[dict],
):
    return {
        "id": 42,
        "tag_name": value.tag,
        "target_commitish": target,
        "name": value.tag,
        "body": publication._release_notes(
            value,
            target=target,
            generator_inputs_sha256="f" * 64,
        ),
        "draft": draft,
        "prerelease": False,
        "immutable": immutable,
        "assets": assets,
        "upload_url": "https://uploads.github.com/repos/proark1/onebrain/"
        "releases/42/assets{?name,label}",
    }


def _set_publication_context(monkeypatch: pytest.MonkeyPatch, *, target: str) -> None:
    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "proark1/onebrain",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": target,
        "GITHUB_WORKFLOW_REF": (
            "proark1/onebrain/.github/workflows/"
            "publish-scanner-definitions.yml@refs/heads/main"
        ),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_publication_guard_is_id_bound_and_has_no_clobber_path():
    script = _PUBLISH_SCRIPT.read_text(encoding="utf-8")
    assert "immutable-releases" in script
    assert '"PATCH"' in script
    assert "releases/{release_id}" in script
    assert "uploads.github.com" in script
    assert '"verify", value.tag' in script
    assert "verify-asset" in script
    assert "branches/main" in script
    assert "GITHUB_REF_PROTECTED" in script
    assert "--clobber" not in script
    assert "release\",\n            \"delete" not in script


def test_protected_main_generator_binding_ignores_definition_activation(
    monkeypatch: pytest.MonkeyPatch,
):
    target = "a" * 40
    _set_publication_context(monkeypatch, target=target)
    remote_lock = _lock_value()
    remote_identity = "clamav-prior-baseline"
    remote_lock["definitions"] = {
        "url": (
            "https://github.com/proark1/onebrain/releases/download/"
            f"scanner-definitions-{remote_identity}/"
            f"onebrain-clamav-definitions-{remote_identity}.tar.gz"
        ),
        "sha256": "0" * 64,
        "identity": remote_identity,
        "archive_manifest_sha256": "1" * 64,
        "max_bytes": supply_chain._MAX_ARTIFACT_BYTES,
    }
    remote_lock_bytes = json.dumps(remote_lock).encode("utf-8")

    def fake_api(method: str, endpoint: str, **_kwargs):
        assert method == "GET"
        if endpoint.endswith("/branches/main"):
            return {"name": "main", "protected": True, "commit": {"sha": target}}
        prefix = "repos/proark1/onebrain/contents/"
        assert endpoint.startswith(prefix) and endpoint.endswith(f"?ref={target}")
        relative_path = endpoint[len(prefix) : -len(f"?ref={target}")]
        if relative_path == publication._SUPPLY_CHAIN_LOCK:
            content = remote_lock_bytes
        else:
            content = (_ROOT / relative_path).read_bytes()
        return {
            "type": "file",
            "path": relative_path,
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }

    monkeypatch.setattr(publication, "_api_json", fake_api)

    evidence = publication._verify_protected_generator_inputs(_LOCK, target=target)

    assert len(evidence) == 64
    assert evidence != hashlib.sha256(_LOCK.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("protected", "head"),
    [(False, "a" * 40), (True, "b" * 40)],
)
def test_publication_rejects_unprotected_or_non_head_main(
    monkeypatch: pytest.MonkeyPatch,
    protected: bool,
    head: str,
):
    target = "a" * 40
    _set_publication_context(monkeypatch, target=target)
    monkeypatch.setattr(
        publication,
        "_api_json",
        lambda *_args, **_kwargs: {
            "name": "main",
            "protected": protected,
            "commit": {"sha": head},
        },
    )

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_target_mismatch",
    ):
        publication._verify_protected_generator_inputs(_LOCK, target=target)


def test_publication_rejects_remote_generator_input_drift(
    monkeypatch: pytest.MonkeyPatch,
):
    target = "a" * 40
    _set_publication_context(monkeypatch, target=target)

    def fake_api(method: str, endpoint: str, **_kwargs):
        assert method == "GET"
        if endpoint.endswith("/branches/main"):
            return {"name": "main", "protected": True, "commit": {"sha": target}}
        prefix = "repos/proark1/onebrain/contents/"
        relative_path = endpoint[len(prefix) : -len(f"?ref={target}")]
        content = (_ROOT / relative_path).read_bytes()
        if relative_path == "deploy/scanner-sandbox/freshclam.conf":
            content += b"# remote drift\n"
        return {
            "type": "file",
            "path": relative_path,
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }

    monkeypatch.setattr(publication, "_api_json", fake_api)

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_generator_input_mismatch",
    ):
        publication._verify_protected_generator_inputs(_LOCK, target=target)


def test_publication_state_machine_validates_draft_before_id_bound_publish(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    target = "a" * 40
    asset = _publication_asset(value)
    draft_asset = _publication_asset(value, draft=True)
    draft = _publication_release(
        value,
        target=target,
        draft=True,
        immutable=False,
        assets=[],
    )
    verified_draft = _publication_release(
        value,
        target=target,
        draft=True,
        immutable=False,
        assets=[draft_asset],
    )
    # GitHub may freeze the release in the PATCH response immediately.
    patched = _publication_release(
        value,
        target=target,
        draft=False,
        immutable=True,
        assets=[asset],
    )
    published = copy.deepcopy(patched)
    expected_api = iter(
        (
            ("GET", "repos/proark1/onebrain/immutable-releases", {"enabled": True}),
            ("POST", "repos/proark1/onebrain/releases", draft),
            (
                "POST",
                "https://uploads.github.com/repos/proark1/onebrain/releases/42/"
                f"assets?name={value.artifact.name}",
                draft_asset,
            ),
            ("GET", "repos/proark1/onebrain/releases/42", verified_draft),
            ("PATCH", "repos/proark1/onebrain/releases/42", patched),
            ("GET", "repos/proark1/onebrain/releases/42", published),
            (
                "GET",
                f"repos/proark1/onebrain/git/ref/tags/{value.tag}",
                {"object": {"type": "commit", "sha": target}},
            ),
        )
    )
    events: list[str] = []

    def fake_api(method: str, endpoint: str, **_kwargs):
        expected_method, expected_endpoint, response = next(expected_api)
        assert (method, endpoint) == (expected_method, expected_endpoint)
        events.append(f"api:{method}:{endpoint}")
        return response

    monkeypatch.setattr(publication.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(
        publication,
        "_require_gh_attestation_support",
        lambda: events.append("preflight"),
    )
    monkeypatch.setattr(publication, "_api_json", fake_api)
    monkeypatch.setattr(
        publication,
        "_verify_protected_generator_inputs",
        lambda *_args, **_kwargs: events.append("target-bound") or "f" * 64,
    )
    monkeypatch.setattr(
        publication,
        "_existing_publication",
        lambda _value, **_kwargs: events.append("identity-absent") or None,
    )
    monkeypatch.setattr(
        publication,
        "_retry_attestation",
        lambda command, **_kwargs: events.append(f"attest:{command[2]}"),
    )

    publication.publish(
        value,
        supply_chain_lock=_LOCK,
        target=target,
        confirm_tag=value.tag,
    )

    assert events[0] == "preflight"
    patch_index = events.index("api:PATCH:repos/proark1/onebrain/releases/42")
    draft_get_index = events.index("api:GET:repos/proark1/onebrain/releases/42")
    tag_index = events.index(
        f"api:GET:repos/proark1/onebrain/git/ref/tags/{value.tag}"
    )
    assert draft_get_index < patch_index < tag_index
    assert events[-2:] == ["attest:verify", "attest:verify-asset"]
    with pytest.raises(StopIteration):
        next(expected_api)


def test_exact_uploaded_draft_resume_skips_duplicate_upload(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    target = "a" * 40
    draft_asset = _publication_asset(value, draft=True)
    final_asset = _publication_asset(value)
    verified_draft = _publication_release(
        value,
        target=target,
        draft=True,
        immutable=False,
        assets=[draft_asset],
    )
    published = _publication_release(
        value,
        target=target,
        draft=False,
        immutable=True,
        assets=[final_asset],
    )
    responses = iter(
        (
            {"enabled": True},
            verified_draft,
            published,
            published,
            {"object": {"type": "commit", "sha": target}},
        )
    )
    calls: list[tuple[str, str]] = []

    def fake_api(method: str, endpoint: str, **_kwargs):
        calls.append((method, endpoint))
        return next(responses)

    monkeypatch.setattr(publication.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(publication, "_require_gh_attestation_support", lambda: None)
    monkeypatch.setattr(publication, "_api_json", fake_api)
    monkeypatch.setattr(
        publication,
        "_verify_protected_generator_inputs",
        lambda *_args, **_kwargs: "f" * 64,
    )
    monkeypatch.setattr(
        publication,
        "_existing_publication",
        lambda _value, **_kwargs: publication.ExistingPublication(
            release_id=42,
            draft=True,
            asset_id=7,
            upload_url=(
                "https://uploads.github.com/repos/proark1/onebrain/releases/42/"
                f"assets?name={value.artifact.name}"
            ),
        ),
    )
    monkeypatch.setattr(publication, "_retry_attestation", lambda *_args, **_kwargs: None)

    publication.publish(
        value,
        supply_chain_lock=_LOCK,
        target=target,
        confirm_tag=value.tag,
    )

    assert all(method != "POST" for method, _endpoint in calls)
    assert calls[1:4] == [
        ("GET", "repos/proark1/onebrain/releases/42"),
        ("PATCH", "repos/proark1/onebrain/releases/42"),
        ("GET", "repos/proark1/onebrain/releases/42"),
    ]
    with pytest.raises(StopIteration):
        next(responses)


def test_exact_immutable_publication_rerun_is_verification_only(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    target = "a" * 40
    asset = _publication_asset(value)
    release = _publication_release(
        value,
        target=target,
        draft=False,
        immutable=True,
        assets=[asset],
    )
    api_calls: list[tuple[str, str]] = []
    responses = iter(
        (
            {"enabled": True},
            release,
            {"object": {"type": "commit", "sha": target}},
        )
    )
    attestations: list[str] = []

    def fake_api(method: str, endpoint: str, **_kwargs):
        api_calls.append((method, endpoint))
        return next(responses)

    monkeypatch.setattr(publication.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(publication, "_require_gh_attestation_support", lambda: None)
    monkeypatch.setattr(publication, "_api_json", fake_api)
    monkeypatch.setattr(
        publication,
        "_verify_protected_generator_inputs",
        lambda *_args, **_kwargs: "f" * 64,
    )
    monkeypatch.setattr(
        publication,
        "_existing_publication",
        lambda _value, **_kwargs: publication.ExistingPublication(
            release_id=42,
            draft=False,
        ),
    )
    monkeypatch.setattr(
        publication,
        "_retry_attestation",
        lambda command, **_kwargs: attestations.append(command[2]),
    )

    publication.publish(
        value,
        supply_chain_lock=_LOCK,
        target=target,
        confirm_tag=value.tag,
    )

    assert [method for method, _endpoint in api_calls] == ["GET", "GET", "GET"]
    assert all(method not in {"POST", "PATCH"} for method, _endpoint in api_calls)
    assert attestations == ["verify", "verify-asset"]


@pytest.mark.parametrize("mutation", ["body", "target", "mutable", "extra_asset"])
def test_existing_publication_recovery_rejects_any_terminal_state_drift(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
):
    value = _publication_value()
    target = "a" * 40
    asset = _publication_asset(value)
    release = _publication_release(
        value,
        target=target,
        draft=False,
        immutable=True,
        assets=[asset],
    )
    if mutation == "body":
        release["body"] = "different"
    elif mutation == "target":
        release["target_commitish"] = "b" * 40
    elif mutation == "mutable":
        release["immutable"] = False
    else:
        release["assets"].append(copy.deepcopy(asset))
    monkeypatch.setattr(publication, "_api_json", lambda *_args, **_kwargs: release)
    monkeypatch.setattr(
        publication,
        "_verify_tag_and_attestations",
        lambda *_args, **_kwargs: pytest.fail("drift must fail before attestation"),
    )

    with pytest.raises(publication.PublicationError):
        publication._verify_existing_release(
            value,
            release_id=42,
            target=target,
            expected_body=publication._release_notes(
                value,
                target=target,
                generator_inputs_sha256="f" * 64,
            ),
        )


def test_publication_never_publishes_an_unverified_uploaded_asset(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    target = "a" * 40
    draft = _publication_release(
        value,
        target=target,
        draft=True,
        immutable=False,
        assets=[],
    )
    invalid_asset = _publication_asset(value, draft=True)
    invalid_asset["digest"] = f"sha256:{'0' * 64}"
    calls: list[tuple[str, str]] = []
    responses = iter(({"enabled": True}, draft, invalid_asset))

    def fake_api(method: str, endpoint: str, **_kwargs):
        calls.append((method, endpoint))
        return next(responses)

    monkeypatch.setattr(publication.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(publication, "_require_gh_attestation_support", lambda: None)
    monkeypatch.setattr(publication, "_api_json", fake_api)
    monkeypatch.setattr(
        publication,
        "_verify_protected_generator_inputs",
        lambda *_args, **_kwargs: "f" * 64,
    )
    monkeypatch.setattr(
        publication,
        "_existing_publication",
        lambda _value, **_kwargs: None,
    )

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_asset_invalid",
    ):
        publication.publish(
            value,
            supply_chain_lock=_LOCK,
            target=target,
            confirm_tag=value.tag,
        )

    assert all(method != "PATCH" for method, _endpoint in calls)


def test_incomplete_existing_draft_blocks_publication_identity_reuse(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    monkeypatch.setattr(
        publication,
        "_list_releases",
        lambda: [{"tag_name": value.tag, "draft": True, "assets": []}],
    )

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_release_exists",
    ):
        publication._existing_publication(
            value,
            target="a" * 40,
            expected_body=publication._release_notes(
                value,
                target="a" * 40,
                generator_inputs_sha256="f" * 64,
            ),
        )


def test_exact_existing_draft_is_adopted_for_id_bound_resume(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    target = "a" * 40
    release = _publication_release(
        value,
        target=target,
        draft=True,
        immutable=False,
        assets=[_publication_asset(value, draft=True)],
    )
    monkeypatch.setattr(publication, "_list_releases", lambda: [release])
    absent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        publication,
        "_require_absent",
        lambda endpoint, *, code: absent.append((endpoint, code)),
    )

    existing = publication._existing_publication(
        value,
        target=target,
        expected_body=publication._release_notes(
            value,
            target=target,
            generator_inputs_sha256="f" * 64,
        ),
    )

    assert existing == publication.ExistingPublication(
        release_id=42,
        draft=True,
        asset_id=7,
        upload_url=(
            "https://uploads.github.com/repos/proark1/onebrain/releases/42/"
            f"assets?name={value.artifact.name}"
        ),
    )
    assert absent == [
        (
            f"repos/proark1/onebrain/git/ref/tags/{value.tag}",
            "definition_publication_tag_exists",
        )
    ]


def test_orphan_tag_blocks_publication_identity_reuse(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    monkeypatch.setattr(publication, "_list_releases", lambda: [])

    def present(_endpoint: str, *, code: str):
        raise publication.PublicationError(code)

    monkeypatch.setattr(publication, "_require_absent", present)

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_tag_exists",
    ):
        publication._existing_publication(
            value,
            target="a" * 40,
            expected_body=publication._release_notes(
                value,
                target="a" * 40,
                generator_inputs_sha256="f" * 64,
            ),
        )


def test_unsupported_gh_attestation_fails_before_release_mutation(
    monkeypatch: pytest.MonkeyPatch,
):
    value = _publication_value()
    calls: list[tuple[str, ...]] = []

    def unsupported(command, **_kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 1, "", "unsupported")

    monkeypatch.setattr(publication.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(publication, "_run_capture", unsupported)
    monkeypatch.setattr(
        publication,
        "_api_json",
        lambda *_args, **_kwargs: pytest.fail("release API must not be called"),
    )

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_gh_attestation_unsupported",
    ):
        publication.publish(
            value,
            supply_chain_lock=_LOCK,
            target="a" * 40,
            confirm_tag=value.tag,
        )

    assert calls == [("gh", "release", "verify", "--help")]


def test_publication_workflows_separate_immutable_publish_and_online_refresh():
    publish_workflow = (
        _ROOT / ".github" / "workflows" / "publish-scanner-definitions.yml"
    ).read_text(encoding="utf-8")
    refresh_workflow = (
        _ROOT / ".github" / "workflows" / "scanner-runtime-refresh-smoke.yml"
    ).read_text(encoding="utf-8")
    image_workflow = (
        _ROOT / ".github" / "workflows" / "publish-images.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in publish_workflow
    assert "environment: scanner-definitions" in publish_workflow
    assert "if: github.ref == 'refs/heads/main'" in publish_workflow
    assert "contents: write" in publish_workflow
    assert "--target \"${GITHUB_SHA}\"" in publish_workflow
    assert "--supply-chain-lock" in publish_workflow
    assert "schedule:" in refresh_workflow
    assert "--target scanner-runtime-refresh-validation" in refresh_workflow
    assert "contents: write" not in refresh_workflow
    assert "workflow_call:" in image_workflow
    assert "workflow_dispatch:" not in image_workflow


def test_publication_input_revalidates_archive_manifest_name_and_digest(tmp_path: Path):
    archive, manifest_bytes, _paths = _archive_fixture(tmp_path)
    identity = "clamav-fixture"
    artifact = tmp_path / f"onebrain-clamav-definitions-{identity}.tar.gz"
    archive.replace(artifact)
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    fragment = tmp_path / "fragment.json"
    fragment.write_text(
        json.dumps(
            {
                "url": (
                    "https://github.com/proark1/onebrain/releases/download/"
                    f"scanner-definitions-{identity}/{artifact.name}"
                ),
                "sha256": artifact_sha256,
                "identity": identity,
                "archive_manifest_sha256": hashlib.sha256(
                    manifest_bytes
                ).hexdigest(),
                "max_bytes": supply_chain._MAX_ARTIFACT_BYTES,
            }
        ),
        encoding="utf-8",
    )

    value = publication.validate_input(artifact, fragment)

    assert value.tag == "scanner-definitions-clamav-fixture"
    assert value.sha256 == artifact_sha256


def test_publication_confirmation_is_required_before_any_external_call(
    monkeypatch: pytest.MonkeyPatch,
):
    value = publication.PublicationInput(
        artifact=Path("artifact.tar.gz"),
        tag="scanner-definitions-clamav-fixture",
        identity="clamav-fixture",
        sha256="1" * 64,
        manifest_sha256="2" * 64,
        size=1,
    )
    called = False

    def unexpected(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("external command must not run")

    monkeypatch.setattr(publication, "_api_json", unexpected)

    with pytest.raises(
        publication.PublicationError,
        match="definition_publication_confirmation_mismatch",
    ):
        publication.publish(
            value,
            supply_chain_lock=_LOCK,
            target="a" * 40,
            confirm_tag="scanner-definitions-wrong",
        )

    assert called is False

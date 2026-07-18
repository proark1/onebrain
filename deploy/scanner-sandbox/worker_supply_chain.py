#!/usr/bin/env python3
"""Strict worker image supply-chain lock, installer, and artifact tooling.

Production Docker stages call this module instead of invoking APT or a live
definition mirror directly.  The only command that contacts the ClamAV mirror
is ``bootstrap-definitions``; it exists solely in a disposable, explicitly
targeted Docker stage used to publish a new immutable baseline artifact.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping, Sequence


_STAGES = (
    "scanner-launcher-build",
    "worker-runtime",
    "scanner-validation",
)
_SNAPSHOTS = ("debian", "debian-security")
_DATABASE_STEMS = ("main", "daily", "bytecode")
_LOCK_FIELDS = {
    "schema",
    "base_image",
    "debian",
    "stages",
    "definitions",
    "updated_at",
}
_BASE_FIELDS = {"reference", "linux_amd64_manifest_digest"}
_DEBIAN_FIELDS = {"suite", "architecture", "keyring", "snapshots"}
_SNAPSHOT_FIELDS = {"url", "suite", "components"}
_STAGE_FIELDS = {"packages", "inventory_sha256"}
_PACKAGE_FIELDS = {"name", "version"}
_DEFINITION_FIELDS = {
    "url",
    "sha256",
    "identity",
    "archive_manifest_sha256",
    "max_bytes",
}
_ARCHIVE_MANIFEST_FIELDS = {"schema", "identity", "files"}
_ARCHIVE_FILE_FIELDS = {"sha256", "size"}
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_OCI_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~\-]*")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_INVENTORY_RECORD_RE = re.compile(
    r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?=[^\s=][^\r\n]{0,255}"
)
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 4
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_DEFINITION_MEMBER_BYTES = 384 * 1024 * 1024
_MAX_EXPANDED_DEFINITION_BYTES = 512 * 1024 * 1024
_ARCHIVE_MANIFEST_NAME = "manifest.json"


class SupplyChainError(RuntimeError):
    """A stable, content-free lock or artifact error."""


@dataclass(frozen=True)
class PackagePin:
    name: str
    version: str

    @property
    def apt_spec(self) -> str:
        return f"{self.name}={self.version}"


@dataclass(frozen=True)
class StageLock:
    packages: tuple[PackagePin, ...]
    inventory_sha256: str


@dataclass(frozen=True)
class SnapshotLock:
    url: str
    suite: str
    components: tuple[str, ...]


@dataclass(frozen=True)
class DefinitionArtifactLock:
    url: str
    sha256: str
    identity: str
    archive_manifest_sha256: str
    max_bytes: int


@dataclass(frozen=True)
class WorkerSupplyChainLock:
    path: Path
    base_reference: str
    linux_amd64_manifest_digest: str
    suite: str
    architecture: str
    keyring: str
    snapshots: Mapping[str, SnapshotLock]
    stages: Mapping[str, StageLock]
    definitions: DefinitionArtifactLock
    updated_at: str

    @property
    def sha256(self) -> str:
        return _sha256_file(self.path)


def load_lock(path: Path) -> WorkerSupplyChainLock:
    raw = _load_json(path, code="supply_chain_lock_invalid")
    _require_fields(raw, _LOCK_FIELDS, code="supply_chain_lock_fields")
    if raw["schema"] != 1:
        raise SupplyChainError("supply_chain_lock_schema")

    base = _object(raw["base_image"], code="supply_chain_base_invalid")
    _require_fields(base, _BASE_FIELDS, code="supply_chain_base_fields")
    reference = _string(base["reference"], code="supply_chain_base_invalid")
    if not re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", reference):
        raise SupplyChainError("supply_chain_base_invalid")
    manifest_digest = _string(
        base["linux_amd64_manifest_digest"], code="supply_chain_base_invalid"
    )
    if not _OCI_DIGEST_RE.fullmatch(manifest_digest):
        raise SupplyChainError("supply_chain_base_invalid")

    debian = _object(raw["debian"], code="supply_chain_debian_invalid")
    _require_fields(debian, _DEBIAN_FIELDS, code="supply_chain_debian_fields")
    suite = _safe_token(debian["suite"], code="supply_chain_debian_invalid")
    architecture = _safe_token(
        debian["architecture"], code="supply_chain_debian_invalid"
    )
    if architecture != "amd64":
        raise SupplyChainError("supply_chain_architecture_unsupported")
    keyring = _string(debian["keyring"], code="supply_chain_debian_invalid")
    keyring_path = PurePosixPath(keyring)
    if not keyring_path.is_absolute() or ".." in keyring_path.parts:
        raise SupplyChainError("supply_chain_keyring_invalid")
    snapshots_raw = _object(
        debian["snapshots"], code="supply_chain_snapshots_invalid"
    )
    if set(snapshots_raw) != set(_SNAPSHOTS):
        raise SupplyChainError("supply_chain_snapshots_invalid")
    snapshots: dict[str, SnapshotLock] = {}
    for name in _SNAPSHOTS:
        value = _object(snapshots_raw[name], code="supply_chain_snapshot_invalid")
        _require_fields(value, _SNAPSHOT_FIELDS, code="supply_chain_snapshot_fields")
        url = _snapshot_url(value["url"], name=name)
        snapshot_suite = _safe_token(
            value["suite"], code="supply_chain_snapshot_invalid"
        )
        components_raw = value["components"]
        if not isinstance(components_raw, list) or not components_raw:
            raise SupplyChainError("supply_chain_snapshot_invalid")
        components = tuple(
            _safe_token(item, code="supply_chain_snapshot_invalid")
            for item in components_raw
        )
        if len(components) != len(set(components)):
            raise SupplyChainError("supply_chain_snapshot_invalid")
        snapshots[name] = SnapshotLock(
            url=url,
            suite=snapshot_suite,
            components=components,
        )
    if snapshots["debian"].suite != suite:
        raise SupplyChainError("supply_chain_suite_mismatch")

    stages_raw = _object(raw["stages"], code="supply_chain_stages_invalid")
    if set(stages_raw) != set(_STAGES):
        raise SupplyChainError("supply_chain_stages_invalid")
    stages: dict[str, StageLock] = {}
    for name in _STAGES:
        value = _object(stages_raw[name], code="supply_chain_stage_invalid")
        _require_fields(value, _STAGE_FIELDS, code="supply_chain_stage_fields")
        package_values = value["packages"]
        if not isinstance(package_values, list) or not package_values:
            raise SupplyChainError("supply_chain_packages_invalid")
        packages: list[PackagePin] = []
        for package_value in package_values:
            package = _object(package_value, code="supply_chain_package_invalid")
            _require_fields(package, _PACKAGE_FIELDS, code="supply_chain_package_fields")
            package_name = _string(
                package["name"], code="supply_chain_package_invalid"
            )
            version = _string(
                package["version"], code="supply_chain_package_invalid"
            )
            if not _PACKAGE_RE.fullmatch(package_name) or not _VERSION_RE.fullmatch(
                version
            ):
                raise SupplyChainError("supply_chain_package_invalid")
            packages.append(PackagePin(package_name, version))
        package_names = [package.name for package in packages]
        if package_names != sorted(package_names) or len(package_names) != len(
            set(package_names)
        ):
            raise SupplyChainError("supply_chain_packages_not_canonical")
        inventory_sha256 = _digest(
            value["inventory_sha256"], code="supply_chain_inventory_invalid"
        )
        stages[name] = StageLock(tuple(packages), inventory_sha256)

    definition_raw = _object(
        raw["definitions"], code="supply_chain_definitions_invalid"
    )
    _require_fields(
        definition_raw,
        _DEFINITION_FIELDS,
        code="supply_chain_definitions_fields",
    )
    identity = _string(
        definition_raw["identity"], code="supply_chain_definition_identity_invalid"
    )
    if not _IDENTITY_RE.fullmatch(identity):
        raise SupplyChainError("supply_chain_definition_identity_invalid")
    url = _definition_url(definition_raw["url"], identity=identity)
    maximum = definition_raw["max_bytes"]
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 1024
        or maximum > _MAX_ARTIFACT_BYTES
    ):
        raise SupplyChainError("supply_chain_definition_size_invalid")
    definitions = DefinitionArtifactLock(
        url=url,
        sha256=_digest(
            definition_raw["sha256"], code="supply_chain_definition_digest_invalid"
        ),
        identity=identity,
        archive_manifest_sha256=_digest(
            definition_raw["archive_manifest_sha256"],
            code="supply_chain_definition_manifest_digest_invalid",
        ),
        max_bytes=maximum,
    )

    updated_at = _string(raw["updated_at"], code="supply_chain_updated_at_invalid")
    try:
        parsed_updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupplyChainError("supply_chain_updated_at_invalid") from exc
    if parsed_updated.tzinfo is None:
        raise SupplyChainError("supply_chain_updated_at_invalid")

    return WorkerSupplyChainLock(
        path=path.resolve(),
        base_reference=reference,
        linux_amd64_manifest_digest=manifest_digest,
        suite=suite,
        architecture=architecture,
        keyring=keyring,
        snapshots=snapshots,
        stages=stages,
        definitions=definitions,
        updated_at=updated_at,
    )


def install_stage(
    lock: WorkerSupplyChainLock,
    stage_name: str,
    *,
    inventory_output: Path | None = None,
) -> str:
    if stage_name not in lock.stages:
        raise SupplyChainError("supply_chain_stage_unknown")
    if os.name == "nt" or not Path("/etc/debian_version").is_file():
        raise SupplyChainError("supply_chain_debian_required")
    architecture = _command_output(["dpkg", "--print-architecture"]).strip()
    if architecture != lock.architecture:
        raise SupplyChainError("supply_chain_architecture_mismatch")
    os_release = _parse_os_release(Path("/etc/os-release"))
    if os_release.get("VERSION_CODENAME") != lock.suite:
        raise SupplyChainError("supply_chain_suite_mismatch")
    keyring = Path(lock.keyring)
    if not keyring.is_file():
        raise SupplyChainError("supply_chain_keyring_missing")

    _replace_apt_sources(lock)
    environment = os.environ.copy()
    environment.update(
        {
            "DEBIAN_FRONTEND": "noninteractive",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    stage = lock.stages[stage_name]
    try:
        _run(["apt-get", "update"], environment=environment)
        _run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *[package.apt_spec for package in stage.packages],
            ],
            environment=environment,
        )
        inventory = normalized_inventory()
        actual_hash = hashlib.sha256(inventory).hexdigest()
        if actual_hash != stage.inventory_sha256:
            print(
                f"stage={stage_name} expected={stage.inventory_sha256} actual={actual_hash}",
                file=sys.stderr,
            )
            raise SupplyChainError("supply_chain_inventory_mismatch")
        if inventory_output is not None:
            _atomic_write(inventory_output, inventory, mode=0o444)
        return actual_hash
    finally:
        shutil.rmtree("/var/lib/apt/lists", ignore_errors=True)
        Path("/var/lib/apt/lists").mkdir(parents=True, exist_ok=True)


def normalized_inventory() -> bytes:
    output = _command_output(
        ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\\n"]
    )
    return _canonical_inventory(output.encode("utf-8"))


def _canonical_inventory(value: bytes) -> bytes:
    try:
        raw_records = [line for line in value.decode("utf-8").splitlines() if line]
    except UnicodeError as exc:
        raise SupplyChainError("supply_chain_inventory_invalid") from exc
    records = sorted(raw_records)
    if (
        not records
        or len(records) != len(set(records))
        or any(not _INVENTORY_RECORD_RE.fullmatch(record) for record in records)
    ):
        raise SupplyChainError("supply_chain_inventory_invalid")
    return ("\n".join(records) + "\n").encode("utf-8")


def fetch_definitions(lock: WorkerSupplyChainLock, destination: Path) -> None:
    artifact = lock.definitions
    destination = destination.resolve()
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise SupplyChainError("definition_destination_not_empty")
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    archive_path = temporary_directory / "artifact.tar.gz"
    extracted_path = temporary_directory / "definitions"
    extracted_path.mkdir(mode=0o700)
    try:
        _download_https(
            artifact.url,
            archive_path,
            expected_sha256=artifact.sha256,
            max_bytes=artifact.max_bytes,
        )
        _extract_definition_archive(
            archive_path,
            extracted_path,
            identity=artifact.identity,
            expected_manifest_sha256=artifact.archive_manifest_sha256,
        )
        archive_path.unlink()
        os.replace(extracted_path, destination)
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)


def bootstrap_definitions(
    output_directory: Path,
    *,
    freshclam_config: Path,
    repository_url: str,
) -> dict:
    """Create a deterministic candidate asset outside the production graph."""

    with tempfile.TemporaryDirectory(prefix="onebrain-definitions-") as temporary:
        definitions = Path(temporary) / "definitions"
        definitions.mkdir(mode=0o700)
        _run(
            [
                "/usr/bin/freshclam",
                f"--config-file={freshclam_config}",
                f"--datadir={definitions}",
                "--stdout",
                "--no-warnings",
            ],
            timeout=15 * 60,
        )
        return package_definitions(
            definitions,
            output_directory,
            repository_url=repository_url,
        )


def package_definitions(
    definitions: Path,
    output_directory: Path,
    *,
    repository_url: str,
) -> dict:
    """Validate an exported official database set and create its release asset."""

    parsed_repository = urllib.parse.urlsplit(repository_url)
    if (
        parsed_repository.scheme != "https"
        or parsed_repository.netloc != "github.com"
        or parsed_repository.path.rstrip("/") != "/proark1/onebrain"
        or parsed_repository.query
        or parsed_repository.fragment
    ):
        raise SupplyChainError("definition_repository_invalid")
    output_directory.mkdir(parents=True, exist_ok=True)
    if any(output_directory.iterdir()):
        raise SupplyChainError("definition_bootstrap_output_not_empty")
    database_paths = _canonical_database_paths(definitions)
    for database in database_paths:
        _run(["/usr/bin/sigtool", "--info", str(database)], timeout=60)
    file_records = {
        path.name: {"sha256": _sha256_file(path), "size": path.stat().st_size}
        for path in database_paths
    }
    identity_seed = _canonical_json({"schema": 1, "files": file_records})
    identity = f"clamav-{hashlib.sha256(identity_seed).hexdigest()[:24]}"
    manifest = {"schema": 1, "identity": identity, "files": file_records}
    manifest_bytes = _canonical_json(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    asset_name = f"onebrain-clamav-definitions-{identity}.tar.gz"
    asset_path = output_directory / asset_name
    _write_deterministic_archive(
        asset_path,
        database_paths=database_paths,
        manifest_bytes=manifest_bytes,
    )
    asset_sha256 = _sha256_file(asset_path)
    tag = f"scanner-definitions-{identity}"
    url = f"{repository_url.rstrip('/')}/releases/download/{tag}/{asset_name}"
    fragment = {
        "url": url,
        "sha256": asset_sha256,
        "identity": identity,
        "archive_manifest_sha256": manifest_sha256,
        "max_bytes": _MAX_ARTIFACT_BYTES,
    }
    _atomic_write(
        output_directory / f"{asset_name}.sha256",
        f"{asset_sha256}  {asset_name}\n".encode("ascii"),
    )
    _atomic_write(
        output_directory / "worker-supply-chain-definition-fragment.json",
        _canonical_json(fragment),
    )
    return fragment


def supply_chain_evidence(
    lock: WorkerSupplyChainLock, *, stage_name: str
) -> dict:
    if stage_name not in lock.stages:
        raise SupplyChainError("supply_chain_stage_unknown")
    return {
        "schema": 1,
        "lock_sha256": lock.sha256,
        "base_image": lock.base_reference,
        "linux_amd64_manifest_digest": lock.linux_amd64_manifest_digest,
        "debian_suite": lock.suite,
        "architecture": lock.architecture,
        "snapshots": {
            name: {
                "url": snapshot.url,
                "suite": snapshot.suite,
            }
            for name, snapshot in sorted(lock.snapshots.items())
        },
        "stage": stage_name,
        "inventory_sha256": lock.stages[stage_name].inventory_sha256,
        "definition_artifact": {
            "url": lock.definitions.url,
            "sha256": lock.definitions.sha256,
            "identity": lock.definitions.identity,
            "archive_manifest_sha256": lock.definitions.archive_manifest_sha256,
        },
    }


def verify_supply_chain_evidence(
    lock: WorkerSupplyChainLock,
    evidence_file: Path,
    *,
    inventory_file: Path,
) -> dict:
    expected = supply_chain_evidence(lock, stage_name="worker-runtime")
    actual = _load_json(evidence_file, code="supply_chain_evidence_invalid")
    if actual != expected:
        raise SupplyChainError("supply_chain_evidence_mismatch")
    try:
        inventory = inventory_file.read_bytes()
    except OSError as exc:
        raise SupplyChainError("supply_chain_inventory_invalid") from exc
    if inventory != _canonical_inventory(inventory):
        raise SupplyChainError("supply_chain_inventory_not_canonical")
    if hashlib.sha256(inventory).hexdigest() != expected["inventory_sha256"]:
        raise SupplyChainError("supply_chain_inventory_mismatch")
    return actual


def _dockerfile_instructions(text: str) -> list[tuple[str, str, str]]:
    """Return normalized logical instructions while skipping heredoc bodies."""

    lines = text.splitlines()
    instructions: list[tuple[str, str, str]] = []
    index = 0
    heredoc_pattern = re.compile(
        r"<<(-?)(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z0-9_.-]+))"
    )
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts: list[str] = []
        while True:
            stripped = raw.rstrip()
            continued = stripped.endswith("\\")
            parts.append(stripped[:-1] if continued else stripped)
            if not continued:
                break
            if index >= len(lines):
                raise SupplyChainError("supply_chain_dockerfile_instruction_invalid")
            raw = lines[index]
            index += 1
        logical = " ".join(part.strip() for part in parts if part.strip())
        match = re.fullmatch(r"([A-Za-z]+)(?:\s+(.*))?", logical, re.DOTALL)
        if match is None:
            raise SupplyChainError("supply_chain_dockerfile_instruction_invalid")
        keyword = match.group(1).upper()
        value = match.group(2) or ""
        heredoc_body: list[str] = []
        for heredoc in heredoc_pattern.finditer(logical):
            strip_tabs = heredoc.group(1) == "-"
            delimiter = next(group for group in heredoc.groups()[1:] if group)
            while index < len(lines):
                candidate = lines[index]
                index += 1
                if strip_tabs:
                    candidate = candidate.lstrip("\t")
                if candidate == delimiter:
                    break
                heredoc_body.append(candidate)
            else:
                raise SupplyChainError("supply_chain_dockerfile_instruction_invalid")
        if heredoc_body:
            body = "\n".join(heredoc_body)
            value = f"{value}\n{body}"
            logical = f"{logical}\n{body}"
        instructions.append((keyword, value, logical))
    return instructions


def verify_dockerfile(lock: WorkerSupplyChainLock, dockerfile: Path) -> None:
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SupplyChainError("supply_chain_dockerfile_unreadable") from exc
    instructions = _dockerfile_instructions(text)
    stages: set[str] = set()
    stage_dependencies: dict[str, set[str]] = {}
    stage_instructions: dict[str, list[tuple[str, str, str]]] = {}
    pending_copies: list[tuple[str, str]] = []
    current_stage: str | None = None
    base_count = 0
    scratch_count = 0
    for keyword, value, logical in instructions:
        if keyword == "FROM":
            match = re.fullmatch(
                r"([^\s]+)(?:\s+AS\s+([^\s]+))?",
                value,
                flags=re.IGNORECASE,
            )
            if match is None or match.group(2) is None:
                raise SupplyChainError("supply_chain_dockerfile_from_invalid")
            image, alias = match.groups()
            normalized_image = image.lower()
            normalized_alias = alias.lower()
            if image == lock.base_reference:
                base_count += 1
            elif normalized_image == "scratch":
                scratch_count += 1
            elif normalized_image not in stages:
                raise SupplyChainError("supply_chain_dockerfile_external_base")
            if normalized_alias in stages:
                raise SupplyChainError("supply_chain_dockerfile_stage_duplicate")
            stage_dependencies[normalized_alias] = (
                {normalized_image} if normalized_image in stages else set()
            )
            stage_instructions[normalized_alias] = []
            stages.add(normalized_alias)
            current_stage = normalized_alias
            continue
        if current_stage is None:
            raise SupplyChainError("supply_chain_dockerfile_instruction_invalid")
        stage_instructions[current_stage].append((keyword, value, logical))
        if keyword == "ADD":
            raise SupplyChainError("supply_chain_dockerfile_add_forbidden")
        if keyword == "RUN" and re.search(
            r"(?:^|\s)--mount(?:=|\s|$)", value, flags=re.IGNORECASE
        ):
            raise SupplyChainError("supply_chain_dockerfile_run_mount_forbidden")
        if keyword == "COPY":
            copy_match = re.search(
                r"(?:^|\s)--from(?:=|\s+)([^\s]+)",
                value,
                flags=re.IGNORECASE,
            )
            if copy_match is not None:
                pending_copies.append((current_stage, copy_match.group(1).lower()))
    if not stages:
        raise SupplyChainError("supply_chain_dockerfile_from_invalid")
    for destination, source in pending_copies:
        if source not in stages:
            raise SupplyChainError("supply_chain_dockerfile_external_copy")
        stage_dependencies[destination].add(source)
    if base_count != 1:
        raise SupplyChainError("supply_chain_dockerfile_base_mismatch")
    if scratch_count != 1:
        raise SupplyChainError("supply_chain_dockerfile_scratch_mismatch")
    lowered = text.lower()
    forbidden = (
        "apt-get",
        "apt ",
        "deb.debian.org",
        "security.debian.org",
        "trusted=yes",
        "allow-unauthenticated",
        "allowinsecure",
        "allow-downgrade-to-insecure",
    )
    if any(token in lowered for token in forbidden):
        raise SupplyChainError("supply_chain_dockerfile_unlocked_package_path")
    required = (
        "worker_supply_chain.py install-stage",
        "worker_supply_chain.py fetch-definitions",
        "worker_supply_chain.py verify-lock",
        "AS scanner-definition-artifact-bootstrap",
        "FROM scratch AS scanner-definition-artifact-bootstrap",
        "COPY --from=scanner-definition-artifact-build /out/ /",
        "AS scanner-validation",
        "FROM worker-runtime AS final",
    )
    if any(token not in text for token in required):
        raise SupplyChainError("supply_chain_dockerfile_contract_missing")
    if re.search(r"FROM\s+scanner-definition-artifact-bootstrap\b", text):
        raise SupplyChainError("supply_chain_bootstrap_in_production_graph")
    if "final" not in stage_dependencies:
        raise SupplyChainError("supply_chain_dockerfile_contract_missing")
    final_graph: set[str] = set()
    pending = ["final"]
    while pending:
        stage = pending.pop()
        if stage in final_graph:
            continue
        final_graph.add(stage)
        pending.extend(stage_dependencies[stage])
    forbidden_stages = {
        "scanner-definition-artifact-build",
        "scanner-definition-artifact-bootstrap",
        "scanner-runtime-refresh-validation",
    }
    if final_graph & forbidden_stages:
        raise SupplyChainError("supply_chain_network_stage_in_production_graph")
    forbidden_run = re.compile(
        r"(?:bootstrap-definitions|definitions-update|\bfreshclam\b)",
        flags=re.IGNORECASE,
    )
    if any(
        keyword == "RUN" and forbidden_run.search(value)
        for stage in final_graph
        for keyword, value, _logical in stage_instructions.get(stage, [])
    ):
        raise SupplyChainError("supply_chain_network_command_in_production_graph")


def verify_base_image_index(lock: WorkerSupplyChainLock, raw_index: bytes) -> str:
    """Bind the pinned multi-platform index to its locked Linux/amd64 child."""

    base_digest = lock.base_reference.rsplit("@sha256:", 1)[-1]
    candidates = (raw_index, raw_index[:-1]) if raw_index.endswith(b"\n") else (raw_index,)
    if raw_index.endswith(b"\r\n"):
        candidates = (raw_index, raw_index[:-2])
    verified_raw = next(
        (
            candidate
            for candidate in candidates
            if hashlib.sha256(candidate).hexdigest() == base_digest
        ),
        None,
    )
    if verified_raw is None:
        raise SupplyChainError("supply_chain_base_index_digest_mismatch")
    index = _load_json_bytes(verified_raw, code="supply_chain_base_index_invalid")
    if index.get("schemaVersion") != 2:
        raise SupplyChainError("supply_chain_base_index_invalid")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise SupplyChainError("supply_chain_base_index_invalid")
    matches: list[str] = []
    for descriptor in manifests:
        if not isinstance(descriptor, dict):
            raise SupplyChainError("supply_chain_base_index_invalid")
        digest = descriptor.get("digest")
        platform = descriptor.get("platform")
        if not isinstance(platform, dict) or not _OCI_DIGEST_RE.fullmatch(
            str(digest or "")
        ):
            raise SupplyChainError("supply_chain_base_index_invalid")
        if (
            platform.get("os") == "linux"
            and platform.get("architecture") == "amd64"
            and platform.get("variant") in {None, ""}
        ):
            matches.append(str(digest))
    if len(matches) != 1:
        raise SupplyChainError("supply_chain_base_index_platform_ambiguous")
    if matches[0] != lock.linux_amd64_manifest_digest:
        raise SupplyChainError("supply_chain_base_manifest_digest_mismatch")
    return matches[0]


def _replace_apt_sources(lock: WorkerSupplyChainLock) -> None:
    source_root = Path("/etc/apt/sources.list.d")
    source_root.mkdir(parents=True, exist_ok=True)
    Path("/etc/apt/sources.list").unlink(missing_ok=True)
    for path in source_root.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    entries: list[str] = []
    for name in _SNAPSHOTS:
        snapshot = lock.snapshots[name]
        entries.extend(
            (
                "Types: deb",
                f"URIs: {snapshot.url}",
                f"Suites: {snapshot.suite}",
                f"Components: {' '.join(snapshot.components)}",
                f"Architectures: {lock.architecture}",
                f"Signed-By: {lock.keyring}",
                "Check-Valid-Until: no",
                "",
            )
        )
    _atomic_write(
        source_root / "onebrain-snapshot.sources",
        ("\n".join(entries).rstrip() + "\n").encode("utf-8"),
        mode=0o644,
    )


def _download_https(
    url: str,
    destination: Path,
    *,
    expected_sha256: str,
    max_bytes: int,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OneBrain-worker-supply-chain/1"},
        method="GET",
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https":
                raise SupplyChainError("definition_artifact_redirect_invalid")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > max_bytes:
                raise SupplyChainError("definition_artifact_too_large")
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise SupplyChainError("definition_artifact_too_large")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except SupplyChainError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SupplyChainError("definition_artifact_download_failed") from exc
    if size == 0 or digest.hexdigest() != expected_sha256:
        raise SupplyChainError("definition_artifact_checksum_mismatch")


def _extract_definition_archive(
    archive: Path,
    destination: Path,
    *,
    identity: str,
    expected_manifest_sha256: str,
) -> None:
    try:
        with tarfile.open(archive, mode="r|gz") as bundle:
            seen: set[str] = set()
            files: dict[str, dict] | None = None
            manifest_bytes: bytes | None = None
            member_count = 0
            for member in bundle:
                member_count += 1
                if member_count > _MAX_ARCHIVE_MEMBERS:
                    raise SupplyChainError("definition_artifact_member_limit")
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or len(path.parts) != 1
                    or ".." in path.parts
                    or not member.isreg()
                ):
                    raise SupplyChainError("definition_artifact_member_invalid")
                if member.name in seen:
                    raise SupplyChainError("definition_artifact_duplicate_member")
                seen.add(member.name)
                source = bundle.extractfile(member)
                if source is None:
                    raise SupplyChainError("definition_artifact_member_invalid")
                if member_count == 1:
                    if member.name != _ARCHIVE_MANIFEST_NAME:
                        raise SupplyChainError("definition_artifact_manifest_order")
                    if member.size > _MAX_MANIFEST_BYTES:
                        raise SupplyChainError("definition_artifact_manifest_invalid")
                    manifest_bytes = _read_bounded(
                        source,
                        maximum=_MAX_MANIFEST_BYTES,
                        code="definition_artifact_manifest_invalid",
                    )
                    if len(manifest_bytes) != member.size:
                        raise SupplyChainError("definition_artifact_manifest_invalid")
                    if (
                        hashlib.sha256(manifest_bytes).hexdigest()
                        != expected_manifest_sha256
                    ):
                        raise SupplyChainError(
                            "definition_artifact_manifest_checksum_mismatch"
                        )
                    manifest = _load_json_bytes(
                        manifest_bytes,
                        code="definition_artifact_manifest_invalid",
                    )
                    files = _validate_archive_manifest(manifest, identity=identity)
                    continue
                if files is None or member.name not in files:
                    raise SupplyChainError("definition_artifact_file_set_mismatch")
                record = files[member.name]
                if member.size != record["size"]:
                    raise SupplyChainError("definition_artifact_size_mismatch")
                _copy_verified_member(
                    source,
                    destination / member.name,
                    expected_size=record["size"],
                    expected_sha256=record["sha256"],
                )
            if files is None or manifest_bytes is None:
                raise SupplyChainError("definition_artifact_manifest_missing")
            if seen != set(files) | {_ARCHIVE_MANIFEST_NAME}:
                raise SupplyChainError("definition_artifact_file_set_mismatch")
            _atomic_write(
                destination / _ARCHIVE_MANIFEST_NAME,
                manifest_bytes,
                mode=0o444,
            )
    except SupplyChainError:
        raise
    except (OSError, tarfile.TarError, UnicodeError, ValueError) as exc:
        raise SupplyChainError("definition_artifact_invalid") from exc


def _validate_archive_manifest(value: dict, *, identity: str) -> dict[str, dict]:
    _require_fields(
        value, _ARCHIVE_MANIFEST_FIELDS, code="definition_artifact_manifest_fields"
    )
    if value["schema"] != 1 or value["identity"] != identity:
        raise SupplyChainError("definition_artifact_manifest_identity_mismatch")
    files = _object(value["files"], code="definition_artifact_manifest_invalid")
    stems: set[str] = set()
    validated: dict[str, dict] = {}
    expanded_size = 0
    for name, raw_record in files.items():
        if not isinstance(name, str) or PurePosixPath(name).name != name:
            raise SupplyChainError("definition_artifact_manifest_invalid")
        match = re.fullmatch(r"(main|daily|bytecode)\.(cvd|cld)", name)
        if match is None or match.group(1) in stems:
            raise SupplyChainError("definition_artifact_manifest_invalid")
        stems.add(match.group(1))
        record = _object(raw_record, code="definition_artifact_manifest_invalid")
        _require_fields(
            record,
            _ARCHIVE_FILE_FIELDS,
            code="definition_artifact_manifest_file_fields",
        )
        size = record["size"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > _MAX_DEFINITION_MEMBER_BYTES
        ):
            raise SupplyChainError("definition_artifact_manifest_invalid")
        expanded_size += size
        if expanded_size > _MAX_EXPANDED_DEFINITION_BYTES:
            raise SupplyChainError("definition_artifact_expanded_size_limit")
        validated[name] = {
            "sha256": _digest(
                record["sha256"], code="definition_artifact_manifest_invalid"
            ),
            "size": size,
        }
    if stems != set(_DATABASE_STEMS):
        raise SupplyChainError("definition_artifact_file_set_mismatch")
    return validated


def _copy_verified_member(
    source: BinaryIO,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                raise SupplyChainError("definition_artifact_size_mismatch")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if size != expected_size:
        raise SupplyChainError("definition_artifact_size_mismatch")
    if digest.hexdigest() != expected_sha256:
        raise SupplyChainError("definition_artifact_member_checksum_mismatch")
    destination.chmod(0o444)


def _canonical_database_paths(directory: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for stem in _DATABASE_STEMS:
        matches = [
            path
            for suffix in (".cvd", ".cld")
            if (path := directory / f"{stem}{suffix}").is_file()
        ]
        if len(matches) != 1:
            raise SupplyChainError("definition_bootstrap_file_set_invalid")
        paths.append(matches[0])
    allowed = {path.name for path in paths}
    extras = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".cvd", ".cld"}
    } - allowed
    if extras:
        raise SupplyChainError("definition_bootstrap_file_set_invalid")
    return tuple(paths)


def _write_deterministic_archive(
    output: Path,
    *,
    database_paths: Sequence[Path],
    manifest_bytes: bytes,
) -> None:
    with output.open("xb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                _add_tar_bytes(bundle, _ARCHIVE_MANIFEST_NAME, manifest_bytes)
                for path in sorted(database_paths, key=lambda item: item.name):
                    info = tarfile.TarInfo(path.name)
                    info.size = path.stat().st_size
                    _normalize_tar_info(info)
                    with path.open("rb") as source:
                        bundle.addfile(info, source)


def _add_tar_bytes(bundle: tarfile.TarFile, name: str, value: bytes) -> None:
    import io

    info = tarfile.TarInfo(name)
    info.size = len(value)
    _normalize_tar_info(info)
    bundle.addfile(info, io.BytesIO(value))


def _normalize_tar_info(info: tarfile.TarInfo) -> None:
    info.mode = 0o444
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}


def _snapshot_url(value: object, *, name: str) -> str:
    url = _string(value, code="supply_chain_snapshot_invalid")
    parsed = urllib.parse.urlsplit(url)
    expected_prefix = f"/archive/{name}/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "snapshot.debian.org"
        or not parsed.path.startswith(expected_prefix)
        or not re.fullmatch(
            rf"{re.escape(expected_prefix)}\d{{8}}T\d{{6}}Z/", parsed.path
        )
        or parsed.query
        or parsed.fragment
    ):
        raise SupplyChainError("supply_chain_snapshot_invalid")
    return url


def _definition_url(value: object, *, identity: str) -> str:
    url = _string(value, code="supply_chain_definition_url_invalid")
    parsed = urllib.parse.urlsplit(url)
    asset = f"onebrain-clamav-definitions-{identity}.tar.gz"
    expected = f"/proark1/onebrain/releases/download/scanner-definitions-{identity}/{asset}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != expected
        or parsed.query
        or parsed.fragment
    ):
        raise SupplyChainError("supply_chain_definition_url_invalid")
    return url


def _parse_os_release(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SupplyChainError("supply_chain_os_release_invalid") from exc
    result: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip('"')
    return result


def _load_json(path: Path, *, code: str) -> dict:
    try:
        return _load_json_bytes(path.read_bytes(), code=code)
    except OSError as exc:
        raise SupplyChainError(code) from exc


def _load_json_bytes(value: bytes, *, code: str) -> dict:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SupplyChainError(code) from exc
    return _object(parsed, code=code)


def _object(value: object, *, code: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SupplyChainError(code)
    return value


def _require_fields(value: dict, expected: set[str], *, code: str) -> None:
    if set(value) != expected:
        raise SupplyChainError(code)


def _string(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SupplyChainError(code)
    return value


def _safe_token(value: object, *, code: str) -> str:
    token = _string(value, code=code)
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,63}", token):
        raise SupplyChainError(code)
    return token


def _digest(value: object, *, code: str) -> str:
    digest = _string(value, code=code)
    if not _DIGEST_RE.fullmatch(digest):
        raise SupplyChainError(code)
    return digest


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bounded(source: BinaryIO, *, maximum: int, code: str) -> bytes:
    value = source.read(maximum + 1)
    if len(value) > maximum:
        raise SupplyChainError(code)
    return value


def _atomic_write(path: Path, value: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _command_output(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupplyChainError("supply_chain_command_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 16 * 1024 * 1024:
        raise SupplyChainError("supply_chain_command_failed")
    return completed.stdout.decode("utf-8", errors="strict")


def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> None:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            env=dict(environment) if environment is not None else None,
            check=False,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupplyChainError("supply_chain_command_failed") from exc
    if completed.returncode != 0:
        raise SupplyChainError("supply_chain_command_failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify-lock")
    verify.add_argument("lock", type=Path)
    verify.add_argument("--dockerfile", type=Path)

    base_reference = commands.add_parser("base-reference")
    base_reference.add_argument("lock", type=Path)

    verify_base = commands.add_parser("verify-base-index")
    verify_base.add_argument("lock", type=Path)
    verify_base.add_argument("raw_index", type=Path)

    install = commands.add_parser("install-stage")
    install.add_argument("lock", type=Path)
    install.add_argument("stage", choices=_STAGES)
    install.add_argument("--inventory-output", type=Path)

    fetch = commands.add_parser("fetch-definitions")
    fetch.add_argument("lock", type=Path)
    fetch.add_argument("destination", type=Path)

    bootstrap = commands.add_parser("bootstrap-definitions")
    bootstrap.add_argument("output", type=Path)
    bootstrap.add_argument("--freshclam-config", type=Path, required=True)
    bootstrap.add_argument(
        "--repository-url",
        default="https://github.com/proark1/onebrain",
    )

    package = commands.add_parser("package-definitions")
    package.add_argument("definitions", type=Path)
    package.add_argument("output", type=Path)
    package.add_argument(
        "--repository-url",
        default="https://github.com/proark1/onebrain",
    )

    evidence = commands.add_parser("write-evidence")
    evidence.add_argument("lock", type=Path)
    evidence.add_argument("output", type=Path)
    evidence.add_argument("--stage", choices=_STAGES, required=True)
    evidence.add_argument("--inventory", type=Path, required=True)

    verify_evidence = commands.add_parser("verify-evidence")
    verify_evidence.add_argument("lock", type=Path)
    verify_evidence.add_argument("evidence", type=Path)
    verify_evidence.add_argument("--inventory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "bootstrap-definitions":
        fragment = bootstrap_definitions(
            args.output,
            freshclam_config=args.freshclam_config,
            repository_url=args.repository_url,
        )
        print(json.dumps(fragment, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "package-definitions":
        fragment = package_definitions(
            args.definitions,
            args.output,
            repository_url=args.repository_url,
        )
        print(json.dumps(fragment, sort_keys=True, separators=(",", ":")))
        return 0
    lock = load_lock(args.lock)
    if args.command == "verify-lock":
        if args.dockerfile is not None:
            verify_dockerfile(lock, args.dockerfile)
        print(lock.sha256)
        return 0
    if args.command == "base-reference":
        print(lock.base_reference)
        return 0
    if args.command == "verify-base-index":
        try:
            raw_index = args.raw_index.read_bytes()
        except OSError as exc:
            raise SupplyChainError("supply_chain_base_index_unreadable") from exc
        print(verify_base_image_index(lock, raw_index))
        return 0
    if args.command == "install-stage":
        print(
            install_stage(
                lock,
                args.stage,
                inventory_output=args.inventory_output,
            )
        )
        return 0
    if args.command == "fetch-definitions":
        fetch_definitions(lock, args.destination)
        print(lock.definitions.identity)
        return 0
    if args.command == "write-evidence":
        expected_hash = lock.stages[args.stage].inventory_sha256
        try:
            inventory = args.inventory.read_bytes()
        except OSError as exc:
            raise SupplyChainError("supply_chain_inventory_invalid") from exc
        if inventory != _canonical_inventory(inventory):
            raise SupplyChainError("supply_chain_inventory_not_canonical")
        if hashlib.sha256(inventory).hexdigest() != expected_hash:
            raise SupplyChainError("supply_chain_inventory_mismatch")
        evidence = supply_chain_evidence(lock, stage_name=args.stage)
        _atomic_write(args.output, _canonical_json(evidence), mode=0o444)
        print(evidence["lock_sha256"])
        return 0
    if args.command == "verify-evidence":
        evidence = verify_supply_chain_evidence(
            lock,
            args.evidence,
            inventory_file=args.inventory,
        )
        print(evidence["lock_sha256"])
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupplyChainError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

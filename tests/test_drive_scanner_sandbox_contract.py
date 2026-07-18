from __future__ import annotations

import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SANDBOX = _ROOT / "deploy" / "scanner-sandbox"


def test_scanner_shell_entrypoints_are_lf_normalized_for_linux_images():
    attributes = (_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "deploy/scanner-sandbox/*.sh text eol=lf" in attributes
    for script in _SANDBOX.glob("*.sh"):
        contents = script.read_bytes()
        assert contents.startswith(b"#!/"), script
        assert b"\r" not in contents, script


def test_native_launcher_has_only_the_two_fixed_profiles_and_never_uses_a_shell():
    source = (_SANDBOX / "onebrain_scanner_sandbox.c").read_text(encoding="utf-8")

    assert 'strcmp(argv[1], "scan")' in source
    assert 'strcmp(argv[1], "definitions-update")' in source
    assert 'fail("unknown profile")' in source
    assert "system(" not in source
    assert "popen(" not in source
    assert 'execv(CLAMSCAN_BINARY' in source
    assert 'execv(FRESHCLAM_BINARY' in source


def test_scan_profile_proves_process_filesystem_and_network_restrictions_before_exec():
    source = (_SANDBOX / "onebrain_scanner_sandbox.c").read_text(encoding="utf-8")

    assert "PR_SET_NO_NEW_PRIVS" in source
    assert "PR_SET_DUMPABLE" in source
    assert "landlock_restrict_self" in source
    assert "Landlock ABI 3 or newer is required" in source
    assert "seccomp_load" in source
    for syscall in (
        "socket",
        "connect",
        "ptrace",
        "process_vm_readv",
        "mount",
        "bpf",
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
    ):
        assert f"SCMP_SYS({syscall})" in source
    assert 'DEFINITION_ROOT "/var/lib/onebrain/clamav"' in source
    assert 'SCAN_TEMP_ROOT "/tmp/onebrain-scanner"' in source
    assert "/data/drive" not in source
    assert 'clearenv()' in source
    assert "close_extra_descriptors" in source
    assert 'require_path_denied("/data")' in source
    assert 'require_path_denied("/app/alembic.ini")' in source
    assert 'require_path_denied("/proc/self/environ")' in source
    assert "verify_scan_boundary" in source
    assert "verify_update_boundary" in source
    assert "verify_io_uring_denied();" in source
    assert "errno != EACCES && errno != EPERM && errno != ENOSYS" in source
    for syscall in ("io_uring_setup", "io_uring_enter", "io_uring_register"):
        assert f"syscall(__NR_{syscall}" in source


def test_definition_update_profile_has_network_but_no_drive_or_worker_secret_allowlist():
    source = (_SANDBOX / "onebrain_scanner_sandbox.c").read_text(encoding="utf-8")

    assert 'DEFINITION_INCOMING_ROOT DEFINITION_ROOT "/incoming"' in source
    assert 'allow_path(ruleset_fd, "/etc/ssl/certs", read_access())' in source
    assert "starts_with_path(resolved, DEFINITION_INCOMING_ROOT)" in source
    assert 'getenv("ONEBRAIN_' not in source
    assert 'strncmp(*entry, "ONEBRAIN_", 9)' in source
    assert "definition update sandbox unexpectedly denied socket creation" in source


def test_worker_image_packages_and_smoke_tests_the_scanner_without_a_new_service():
    dockerfile = (_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    supply_lock = json.loads(
        (_SANDBOX / "worker-supply-chain.lock.json").read_text(encoding="utf-8")
    )
    compose_files = list((_ROOT / "deploy").rglob("*compose*.yml")) + list(
        (_ROOT / "deploy").rglob("*compose*.yaml")
    )

    runtime_packages = {
        item["name"]: item["version"]
        for item in supply_lock["stages"]["worker-runtime"]["packages"]
    }
    launcher_packages = {
        item["name"]: item["version"]
        for item in supply_lock["stages"]["scanner-launcher-build"]["packages"]
    }
    assert runtime_packages["clamav"] == "1.4.3+dfsg-1"
    assert runtime_packages["clamav-freshclam"] == "1.4.3+dfsg-1"
    assert launcher_packages["libseccomp-dev"] == "2.6.0-2"
    assert "apt-get" not in dockerfile
    assert "worker_supply_chain.py install-stage" in dockerfile
    assert "worker_supply_chain.py fetch-definitions" in dockerfile
    assert "onebrain-scanner-image-smoke" in dockerfile
    assert "USER onebrain" in dockerfile
    assert "python -m app.drive.malware.definitions manifest" in dockerfile
    assert "AS scanner-validation" in dockerfile
    assert "COPY --from=scanner-validation" in dockerfile
    assert "clamav-testfiles" not in dockerfile
    assert "clam-upx.exe" not in dockerfile
    assert "AS scanner-definition-artifact-bootstrap" in dockerfile
    assert "FROM scanner-definition-artifact-bootstrap" not in dockerfile
    assert "scanner-release.json" in dockerfile
    assert "scanner-packages.txt" in dockerfile
    assert "/usr/bin/ldd" in (
        _ROOT / "app" / "drive" / "malware" / "definitions.py"
    ).read_text(encoding="utf-8")
    assert all("clamd:" not in path.read_text(encoding="utf-8") for path in compose_files)


def test_capability_manifest_matches_the_fail_closed_launcher_contract():
    contract = json.loads((_SANDBOX / "capabilities.json").read_text(encoding="utf-8"))

    assert contract["profiles"] == ["scan", "definitions-update"]
    assert contract["minimum_landlock_abi"] >= 3
    assert contract["scan_network"] == "denied-by-seccomp"
    assert contract["drive_root_access"] == "denied"
    assert contract["required_clamav_options"] == [
        "--official-db-only",
        "--max-scantime",
        "--bytecode-timeout",
        "--alert-exceeds-max",
        "--alert-encrypted",
        "--max-filesize",
        "--max-scansize",
        "--max-files",
        "--max-recursion",
    ]
    assert "packages-sha256" in contract["release_evidence"]
    assert "shared-library-sha256" in contract["release_evidence"]
    assert "supply-chain-lock-sha256" in contract["release_evidence"]
    assert "base-image-digest" in contract["release_evidence"]
    assert "debian-snapshot-identity" in contract["release_evidence"]
    assert "definition-artifact-sha256" in contract["release_evidence"]
    assert "io-uring" in contract["runtime_negative_probes"]


def test_image_gate_exercises_exact_resource_diagnostics_offline():
    smoke = (_SANDBOX / "image-smoke.sh").read_text(encoding="utf-8")
    fixture_builder = (_SANDBOX / "build-fixtures.py").read_text(encoding="utf-8")

    for option in (
        "--official-db-only=yes",
        "--max-scantime=",
        "--bytecode-timeout=",
    ):
        assert option in smoke
    for diagnostic in (
        "MaxRecursion",
        "MaxFiles",
        "MaxFileSize",
        "MaxScanSize",
        "MaxScanTime",
    ):
        assert diagnostic in smoke
    assert "verify-release-evidence" in smoke
    assert '"${fixtures}/file-size.bin"' in smoke
    assert '(root / "file-size.bin").write_bytes(b"F" * 4096)' in fixture_builder
    assert "file-size.zip" not in smoke
    assert "file-size.zip" not in fixture_builder
    assert '"${launcher}" scan' in smoke
    assert "definitions-update" not in smoke
    assert "freshclam" not in smoke.lower()
    assert "bytecode.exe" not in smoke


def test_networked_refresh_probe_is_separate_from_final_image_gate():
    refresh = (_SANDBOX / "runtime-refresh-smoke.sh").read_text(encoding="utf-8")

    assert '"${launcher}" definitions-update "${update_target}"' in refresh
    assert 'cp "${baseline}"/*.cvd' in refresh
    assert 'cp "${baseline}"/*.cld' in refresh
    assert "sigtool --info" in refresh

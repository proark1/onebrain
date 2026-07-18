from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone

import pytest

from app.drive.malware import clamav as clamav_module
from app.drive.malware.base import MalwareScanContractError, ScanRequest
from app.drive.malware.clamav import ClamAVScanner
from app.drive.malware.definitions import DefinitionSnapshot


class _Definitions:
    baseline_dir = __import__("pathlib").Path("/opt/onebrain/clamav-baseline")
    snapshot = DefinitionSnapshot(
        path=__import__("pathlib").Path("/var/lib/onebrain/clamav/sets/test"),
        version="db-27500",
        timestamp=datetime(2026, 7, 18, tzinfo=timezone.utc),
        identity="test",
    )

    def active_snapshot(self, *, require_fresh: bool = True) -> DefinitionSnapshot:
        assert require_fresh
        return self.snapshot

    def initialize(self) -> DefinitionSnapshot:
        return self.snapshot


class _Sink:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, value: bytes) -> int:
        self.chunks.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Process:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", return_code: int = 0):
        self.stdin = _Sink()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.return_code = return_code
        self.terminated = False

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


def _scanner(process: _Process, calls: list[tuple]) -> ClamAVScanner:
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    scanner = ClamAVScanner(
        sandbox_binary="/usr/local/bin/onebrain-scanner-sandbox",
        definitions=_Definitions(),
        timeout_seconds=2,
        max_scan_time_ms=1_000,
        bytecode_timeout_ms=500,
        output_limit_bytes=64,
        max_source_bytes=1024,
        max_scan_bytes=4096,
        max_file_bytes=2048,
        max_archive_files=100,
        max_archive_recursion=4,
        popen_factory=popen,
    )
    scanner._engine_version = "1.4.3"
    return scanner


def _request(chunks=(b"one", b"brain")) -> ScanRequest:
    data = b"".join(chunks)
    return ScanRequest(
        revision_id="revision-1",
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expected_size_bytes=len(data),
        policy_epoch=1,
        content=iter(chunks),
    )


def test_streams_stdin_without_a_shell_and_attests_the_same_chunks():
    process = _Process(stdout=b"stdin: OK\n")
    calls: list[tuple] = []
    scanner = _scanner(process, calls)

    verdict = scanner.scan(_request())

    assert verdict.status == "clean"
    assert process.stdin.chunks == [b"one", b"brain"]
    command, kwargs = calls[0]
    assert command[:2] == ["/usr/local/bin/onebrain-scanner-sandbox", "scan"]
    assert command[-1] == "-"
    assert "--official-db-only=yes" in command
    assert "--max-scantime=1000" in command
    assert "--bytecode-timeout=500" in command
    assert kwargs["shell"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/onebrain-scanner",
        "TMPDIR": "/tmp/onebrain-scanner",
        "LANG": "C",
        "LC_ALL": "C",
    }


@pytest.mark.parametrize(
    ("stdout", "return_code", "status", "code"),
    [
        (b"stdin: Eicar-Signature FOUND\n", 1, "infected", "eicar-signature"),
        (b"stdin: Heuristics.Limits.Exceeded.MaxFileSize FOUND\n", 1, "scan_error", "scan_limit_exceeded"),
        (b"stdin: Heuristics.Encrypted.Zip FOUND\n", 1, "scan_error", "encrypted_content"),
        (b"", 2, "scan_error", "scanner_engine_error"),
        (b"unexpected\n", 0, "scan_error", "scanner_output_invalid"),
    ],
)
def test_maps_only_unambiguous_clamav_output(stdout, return_code, status, code):
    process = _Process(stdout=stdout, return_code=return_code)
    scanner = _scanner(process, [])

    verdict = scanner.scan(_request())

    assert verdict.status == status
    assert (verdict.threat_code or verdict.error_code) == code


def test_excessive_output_is_bounded_and_inconclusive():
    scanner = _scanner(_Process(stdout=b"x" * 1024), [])

    verdict = scanner.scan(_request())

    assert verdict.status == "scan_error"
    assert verdict.error_code == "scanner_output_limit"


def test_stderr_makes_even_an_apparent_detection_inconclusive():
    scanner = _scanner(
        _Process(
            stdout=b"stdin: Example.Threat FOUND\n",
            stderr=b"parser emitted an unexpected warning\n",
            return_code=1,
        ),
        [],
    )

    verdict = scanner.scan(_request())

    assert verdict.status == "scan_error"
    assert verdict.error_code == "scanner_error_output"


def test_non_bytes_stream_chunks_are_a_contract_error():
    scanner = _scanner(_Process(stdout=b"stdin: OK\n"), [])
    request = ScanRequest(
        revision_id="revision-1",
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
        expected_size_bytes=4,
        policy_epoch=1,
        content=("data",),  # type: ignore[arg-type]
    )

    with pytest.raises(MalwareScanContractError, match="invalid_stream_chunk"):
        scanner.scan(request)


def test_integrity_mismatch_never_returns_clean_even_when_clamav_does():
    scanner = _scanner(_Process(stdout=b"stdin: OK\n"), [])
    request = ScanRequest(
        revision_id="revision-1",
        expected_sha256="0" * 64,
        expected_size_bytes=8,
        policy_epoch=1,
        content=(b"different",),
    )

    verdict = scanner.scan(request)

    assert verdict.status == "scan_error"
    assert verdict.error_code == "integrity_mismatch"


def test_worker_startup_verifies_release_evidence_then_runs_full_sandbox_probe(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple] = []
    scanner = _scanner(_Process(stdout=b"stdin: Empty file\n"), calls)
    verified: dict = {}

    def verify(evidence_file, **kwargs):
        verified["evidence_file"] = evidence_file
        verified.update(kwargs)
        return {"engine_version": "1.4.3"}

    monkeypatch.setattr(clamav_module, "verify_scanner_release_evidence", verify)

    scanner.assert_packaged_runtime()

    assert verified["packages_file"].name == "scanner-packages.txt"
    assert verified["supply_chain_file"].name == "worker-supply-chain.json"
    assert verified["baseline_dir"] == _Definitions.baseline_dir
    command, _kwargs = calls[0]
    assert "--official-db-only=yes" in command
    assert "--max-scantime=1000" in command
    assert "--bytecode-timeout=500" in command

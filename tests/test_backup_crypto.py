"""Functional tests for the streaming authenticated customer-backup container."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


HELPER = Path(__file__).parents[1] / "deploy" / "box" / "onebrain_backup_crypto.py"
KEY = "test-only-high-entropy-backup-key-0123456789"


def _run(*args: str, data: bytes = b"", key: str = KEY) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        input=data,
        capture_output=True,
        env={**os.environ, "UPDATE_BACKUP_KEY": key},
        timeout=30,
    )


def test_authenticated_backup_round_trip_streams_binary_content(tmp_path):
    plaintext = (bytes(range(256)) * 8193) + b"tail"
    archive = tmp_path / "backup.obk"

    encrypted = _run("encrypt", "--output", str(archive), data=plaintext)
    assert encrypted.returncode == 0, encrypted.stderr
    assert archive.read_bytes()[:8] == b"OBDBKP02"

    verified = _run("verify", "--input", str(archive))
    assert verified.returncode == 0 and verified.stdout == b""
    decrypted = _run("decrypt", "--input", str(archive))
    assert decrypted.returncode == 0, decrypted.stderr
    assert decrypted.stdout == plaintext


def test_tamper_and_wrong_key_emit_no_plaintext(tmp_path):
    archive = tmp_path / "backup.obk"
    assert _run("encrypt", "--output", str(archive), data=b"sensitive-plaintext" * 100).returncode == 0
    original = archive.read_bytes()

    for name, index in (("ciphertext", 48), ("tag", len(original) - 1)):
        changed = bytearray(original)
        changed[index] ^= 0x01
        candidate = tmp_path / f"{name}.obk"
        candidate.write_bytes(changed)
        result = _run("decrypt", "--input", str(candidate))
        assert result.returncode != 0
        assert result.stdout == b""
        assert b"verification failed" in result.stderr

    wrong_key = _run("decrypt", "--input", str(archive), key="different-backup-key")
    assert wrong_key.returncode != 0
    assert wrong_key.stdout == b""


def test_format_header_is_strict_and_no_plaintext_is_emitted(tmp_path):
    archive = tmp_path / "backup.obk"
    assert _run("encrypt", "--output", str(archive), data=b"private").returncode == 0
    changed = bytearray(archive.read_bytes())
    changed[0] ^= 0x01
    archive.write_bytes(changed)

    result = _run("decrypt", "--input", str(archive))
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"unsupported authenticated backup format" in result.stderr


def test_constant_time_tag_check_precedes_decryptor_creation():
    source = HELPER.read_text(encoding="utf-8")
    verify_body = source[source.index("def _verify_source("):source.index("def verify_file(")]
    decrypt_body = source[source.index("def decrypt_stream("):source.index("def _parser(")]

    assert "hmac.compare_digest" in verify_body
    assert decrypt_body.index("_verify_source(") < decrypt_body.index("decryptor = Cipher")
    assert "target.write" not in decrypt_body[:decrypt_body.index("_verify_source(")]

"""Functional tests for the external authenticated append-only erasure ledger."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


HELPER = Path(__file__).parents[1] / "deploy" / "box" / "onebrain_erasure_ledger.py"
KEY = "test-only-high-entropy-backup-key-0123456789"
DEPLOYMENT = "dep_test"


def _run(command: str, ledger: Path, *, rows: list[dict] | None = None,
         key: str = KEY) -> subprocess.CompletedProcess:
    body = "" if rows is None else "".join(json.dumps(row) + "\n" for row in rows)
    return subprocess.run(
        [sys.executable, str(HELPER), command, "--path", str(ledger),
         "--deployment-id", DEPLOYMENT],
        input=body,
        text=True,
        capture_output=True,
        env={**os.environ, "UPDATE_BACKUP_KEY": key},
        timeout=15,
    )


def _row(seq: int, *, target_ref: str = "") -> dict:
    return {
        "seq": seq,
        "id": f"tomb_{seq}",
        "account_id": "acct_1",
        "space_id": "space_1",
        "target_type": "space" if not target_ref else "subject",
        "target_ref": target_ref,
        "created_at": "2026-07-18T12:00:00Z",
    }


def test_ledger_initializes_appends_and_verifies_as_a_hash_chain(tmp_path):
    ledger = tmp_path / "ledger.ndjson"
    assert _run("init", ledger).stdout.strip() == "0"
    appended = _run("append", ledger, rows=[_row(2), _row(5, target_ref="drive_file:file_1")])
    assert appended.returncode == 0, appended.stderr
    assert appended.stdout.strip() == "5"
    assert _run("verify", ledger).stdout.strip() == "5"

    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in records] == ["genesis", "tombstone", "tombstone"]
    assert records[1]["prev"] == records[0]["mac"]
    assert records[2]["prev"] == records[1]["mac"]
    assert "reason" not in records[1] and "created_by" not in records[1]


def test_ledger_tamper_wrong_key_and_truncation_fail_closed(tmp_path):
    ledger = tmp_path / "ledger.ndjson"
    assert _run("init", ledger).returncode == 0
    assert _run("append", ledger, rows=[_row(1, target_ref="drive_file:file_1")]).returncode == 0

    lines = ledger.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["target_ref"] = "drive_file:resurrect-me"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tampered = _run("verify", ledger)
    assert tampered.returncode != 0
    assert "authentication failed" in tampered.stderr

    # Restore the valid file, then prove the key and complete trailing record are required.
    ledger.unlink()
    assert _run("init", ledger).returncode == 0
    assert _run("append", ledger, rows=[_row(1)]).returncode == 0
    assert _run("verify", ledger, key="wrong-ledger-key").returncode != 0
    ledger.write_bytes(ledger.read_bytes()[:-1])
    assert _run("verify", ledger).returncode != 0


def test_missing_ledger_and_non_increasing_sequence_are_rejected(tmp_path):
    ledger = tmp_path / "ledger.ndjson"
    missing = _run("verify", ledger)
    assert missing.returncode != 0 and "ledger is missing" in missing.stderr

    assert _run("init", ledger).returncode == 0
    assert _run("append", ledger, rows=[_row(3)]).returncode == 0
    duplicate = _run("append", ledger, rows=[_row(3)])
    assert duplicate.returncode != 0
    assert _run("verify", ledger).stdout.strip() == "3"

#!/usr/bin/env python3
"""Authenticated append-only external ledger for OneBrain erasure tombstones."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable


VERSION = 1
ZERO_MAC = "0" * 64
PASSWORD_ENV = "UPDATE_BACKUP_KEY"
MAX_LINE_BYTES = 16 * 1024
RECORD_FIELDS = {
    "seq", "id", "account_id", "space_id", "target_type", "target_ref", "created_at",
}
HEX_MAC = re.compile(r"[0-9a-f]{64}")
GENESIS_PAYLOAD_FIELDS = {"v", "kind", "deployment_id", "seq", "prev"}
TOMBSTONE_PAYLOAD_FIELDS = {
    "v", "kind", "deployment_id", "seq", "id", "account_id", "space_id",
    "target_type", "target_ref", "created_at", "prev",
}


class LedgerError(RuntimeError):
    pass


def _key() -> bytes:
    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        raise LedgerError(f"{PASSWORD_ENV} is required")
    return hmac.new(
        password.encode("utf-8"),
        b"onebrain-drive-erasure-ledger-v1/authentication",
        hashlib.sha256,
    ).digest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _signed(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    return {**payload, "mac": hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()}


def _bounded_text(value: Any, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise LedgerError(f"invalid {field}")
    return value


def _verify(path: Path, deployment_id: str) -> tuple[int, str]:
    if not path.is_file():
        raise LedgerError("external erasure ledger is missing")
    key = _key()
    last_seq = 0
    previous = ZERO_MAC
    saw_genesis = False
    with path.open("rb") as ledger:
        for line_number, raw in enumerate(ledger, start=1):
            if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                raise LedgerError(f"invalid ledger record at line {line_number}")
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerError(f"invalid ledger JSON at line {line_number}") from exc
            if (not isinstance(record, dict) or not isinstance(record.get("mac"), str)
                    or not HEX_MAC.fullmatch(record["mac"])):
                raise LedgerError(f"invalid ledger record at line {line_number}")
            supplied = record.pop("mac")
            expected = hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, supplied):
                raise LedgerError(f"ledger authentication failed at line {line_number}")
            if record.get("v") != VERSION or record.get("deployment_id") != deployment_id:
                raise LedgerError(f"ledger identity mismatch at line {line_number}")
            if record.get("prev") != previous:
                raise LedgerError(f"ledger chain mismatch at line {line_number}")
            kind = record.get("kind")
            if line_number == 1:
                if (set(record) != GENESIS_PAYLOAD_FIELDS or kind != "genesis"
                        or record.get("seq") != 0 or record.get("prev") != ZERO_MAC):
                    raise LedgerError("ledger genesis is invalid")
                saw_genesis = True
            else:
                if set(record) != TOMBSTONE_PAYLOAD_FIELDS or kind != "tombstone":
                    raise LedgerError(f"invalid ledger kind at line {line_number}")
                seq = record.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                    raise LedgerError(f"ledger sequence is invalid at line {line_number}")
                _bounded_text(record.get("id"), "id")
                _bounded_text(record.get("account_id"), "account_id")
                _bounded_text(record.get("space_id"), "space_id")
                _bounded_text(record.get("target_type"), "target_type", 64)
                _bounded_text(record.get("target_ref"), "target_ref", 512)
                _bounded_text(record.get("created_at"), "created_at", 64)
                last_seq = seq
            previous = supplied
    if not saw_genesis:
        raise LedgerError("external erasure ledger has no genesis")
    return last_seq, previous


def init_ledger(path: Path, deployment_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _verify(path, deployment_id)
        return
    payload = {
        "v": VERSION,
        "kind": "genesis",
        "deployment_id": deployment_id,
        "seq": 0,
        "prev": ZERO_MAC,
    }
    line = _canonical(_signed(payload, _key())) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = os.write(descriptor, line)
        if written != len(line):
            raise LedgerError("short ledger genesis write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def _input_records(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"invalid tombstone input at line {number}") from exc
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise LedgerError(f"invalid tombstone fields at line {number}")
        yield record


def append_records(path: Path, deployment_id: str, records: Iterable[dict[str, Any]]) -> int:
    last_seq, previous = _verify(path, deployment_id)
    key = _key()
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        for source in records:
            seq = source.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                raise LedgerError("tombstone sequence is not strictly increasing")
            payload = {
                "v": VERSION,
                "kind": "tombstone",
                "deployment_id": deployment_id,
                "seq": seq,
                "id": _bounded_text(source.get("id"), "id"),
                "account_id": _bounded_text(source.get("account_id"), "account_id"),
                "space_id": _bounded_text(source.get("space_id"), "space_id"),
                "target_type": _bounded_text(source.get("target_type"), "target_type", 64),
                "target_ref": _bounded_text(source.get("target_ref"), "target_ref", 512),
                "created_at": _bounded_text(source.get("created_at"), "created_at", 64),
                "prev": previous,
            }
            signed = _signed(payload, key)
            line = _canonical(signed) + b"\n"
            if len(line) > MAX_LINE_BYTES:
                raise LedgerError("tombstone ledger record is too large")
            written = os.write(descriptor, line)
            if written != len(line):
                raise LedgerError("short append-only ledger write")
            os.fsync(descriptor)
            last_seq = seq
            previous = signed["mac"]
    finally:
        os.close(descriptor)
    return last_seq


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "verify", "append"))
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            init_ledger(args.path, args.deployment_id)
            last_seq = _verify(args.path, args.deployment_id)[0]
        elif args.command == "append":
            last_seq = append_records(
                args.path, args.deployment_id, _input_records(sys.stdin))
        else:
            last_seq = _verify(args.path, args.deployment_id)[0]
        print(last_seq)
        return 0
    except (LedgerError, OSError, ValueError) as exc:
        print(f"OneBrain erasure ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

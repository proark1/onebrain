#!/usr/bin/env python3
"""Streaming authenticated container for customer-local OneBrain backups.

Format v2 is encrypt-then-MAC: AES-256-CTR ciphertext is authenticated with an
independently domain-derived HMAC-SHA256 key. Decryption performs a complete
constant-time tag verification pass before creating a decryptor or emitting a
single plaintext byte.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import struct
import sys
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC = b"OBDBKP02"
KDF_ITERATIONS = 600_000
HEADER = struct.Struct(">8sI16s16s")
TAG_BYTES = hashlib.sha256().digest_size
CHUNK_BYTES = 1024 * 1024
PASSWORD_ENV = "UPDATE_BACKUP_KEY"


class BackupCryptoError(RuntimeError):
    pass


def _password() -> bytes:
    value = os.environ.get(PASSWORD_ENV, "")
    if not value:
        raise BackupCryptoError(f"{PASSWORD_ENV} is required")
    return value.encode("utf-8")


def _derive_keys(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    master = hashlib.pbkdf2_hmac(
        "sha256", password, salt, KDF_ITERATIONS, dklen=32)
    encryption_key = hmac.new(
        master, b"onebrain-drive-backup-v2/encryption", hashlib.sha256).digest()
    authentication_key = hmac.new(
        master, b"onebrain-drive-backup-v2/authentication", hashlib.sha256).digest()
    return encryption_key, authentication_key


def encrypt_stream(source: BinaryIO, output_path: Path) -> None:
    salt = os.urandom(16)
    nonce = os.urandom(16)
    header = HEADER.pack(MAGIC, KDF_ITERATIONS, salt, nonce)
    encryption_key, authentication_key = _derive_keys(_password(), salt)
    authenticator = hmac.new(authentication_key, header, hashlib.sha256)
    encryptor = Cipher(
        algorithms.AES(encryption_key), modes.CTR(nonce)).encryptor()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb", buffering=0) as target:
        target.write(header)
        while True:
            plaintext = source.read(CHUNK_BYTES)
            if not plaintext:
                break
            ciphertext = encryptor.update(plaintext)
            authenticator.update(ciphertext)
            target.write(ciphertext)
        final = encryptor.finalize()
        if final:
            authenticator.update(final)
            target.write(final)
        target.write(authenticator.digest())
        os.fsync(target.fileno())


def _verify_source(source: BinaryIO, size: int) -> tuple[bytes, bytes, int]:
    if size < HEADER.size + TAG_BYTES:
        raise BackupCryptoError("authenticated backup is truncated")
    ciphertext_bytes = size - HEADER.size - TAG_BYTES

    source.seek(0)
    header = source.read(HEADER.size)
    if len(header) != HEADER.size:
        raise BackupCryptoError("authenticated backup is truncated")
    magic, iterations, salt, nonce = HEADER.unpack(header)
    if magic != MAGIC or iterations != KDF_ITERATIONS:
        raise BackupCryptoError("unsupported authenticated backup format")
    encryption_key, authentication_key = _derive_keys(_password(), salt)
    authenticator = hmac.new(authentication_key, header, hashlib.sha256)
    remaining = ciphertext_bytes
    while remaining:
        block = source.read(min(CHUNK_BYTES, remaining))
        if not block:
            raise BackupCryptoError("authenticated backup is truncated")
        remaining -= len(block)
        authenticator.update(block)
    supplied_tag = source.read(TAG_BYTES)
    if len(supplied_tag) != TAG_BYTES or not hmac.compare_digest(
            authenticator.digest(), supplied_tag):
        raise BackupCryptoError("authenticated backup verification failed")
    return encryption_key, nonce, ciphertext_bytes


def verify_file(input_path: Path) -> None:
    with input_path.open("rb", buffering=0) as source:
        _verify_source(source, os.fstat(source.fileno()).st_size)


def decrypt_stream(input_path: Path, target: BinaryIO) -> None:
    # This full pass and constant-time comparison intentionally precede creation
    # of the decryptor and every plaintext write.
    with input_path.open("rb", buffering=0) as source:
        encryption_key, nonce, ciphertext_bytes = _verify_source(
            source, os.fstat(source.fileno()).st_size)
        decryptor = Cipher(
            algorithms.AES(encryption_key), modes.CTR(nonce)).decryptor()
        source.seek(HEADER.size)
        remaining = ciphertext_bytes
        while remaining:
            block = source.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise BackupCryptoError("authenticated backup changed after verification")
            remaining -= len(block)
            target.write(decryptor.update(block))
        final = decryptor.finalize()
        if final:
            target.write(final)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    encrypt = sub.add_parser("encrypt")
    encrypt.add_argument("--output", type=Path, required=True)
    decrypt = sub.add_parser("decrypt")
    decrypt.add_argument("--input", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "encrypt":
            encrypt_stream(sys.stdin.buffer, args.output)
        elif args.command == "decrypt":
            decrypt_stream(args.input, sys.stdout.buffer)
        else:
            verify_file(args.input)
        return 0
    except (BackupCryptoError, OSError, ValueError) as exc:
        print(f"OneBrain backup crypto: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

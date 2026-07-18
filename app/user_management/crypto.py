"""Signed command envelopes and end-to-end encrypted management results."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.trust.signing import sign_payload, verify_payload
from app.user_management.base import USER_MANAGEMENT_ACTIONS, USER_MANAGEMENT_CONTRACT


COMMAND_DOMAIN = b"onebrain:user-management-command:v1\x00"
RESULT_DOMAIN = b"onebrain:user-management-result:v1\x00"


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


@dataclass(frozen=True)
class UserManagementCommand:
    contract: str
    command_id: str
    deployment_id: str
    action: str
    idempotency_key: str
    issued_at: str
    expires_at: str
    result_public_key: str
    payload: dict
    signature: str = ""

    def unsigned(self) -> dict:
        value = asdict(self)
        value.pop("signature", None)
        return value


def generate_result_keypair() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    return _b64(private.private_bytes_raw()), _b64(private.public_key().public_bytes_raw())


def sign_command(command: UserManagementCommand, private_key_b64: str) -> UserManagementCommand:
    if command.contract != USER_MANAGEMENT_CONTRACT or command.action not in USER_MANAGEMENT_ACTIONS:
        raise ValueError("invalid user-management command")
    signature = sign_payload(COMMAND_DOMAIN + _canonical(command.unsigned()), private_key_b64)
    return UserManagementCommand(**command.unsigned(), signature=signature)


def verify_command(command: UserManagementCommand, public_keys: list[str], *, deployment_id: str, now_iso: str) -> bool:
    if command.contract != USER_MANAGEMENT_CONTRACT:
        return False
    if command.action not in USER_MANAGEMENT_ACTIONS or command.deployment_id != deployment_id:
        return False
    if not command.command_id or not command.idempotency_key or command.expires_at <= now_iso:
        return False
    body = COMMAND_DOMAIN + _canonical(command.unsigned())
    return any(verify_payload(body, command.signature, key) for key in public_keys if key)


def result_aad(*, command_id: str, deployment_id: str, action: str) -> bytes:
    return RESULT_DOMAIN + _canonical({
        "action": action,
        "command_id": command_id,
        "deployment_id": deployment_id,
    })


def _result_key(private_key: X25519PrivateKey, public_key: X25519PublicKey, *, aad: bytes) -> bytes:
    shared = private_key.exchange(public_key)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=aad).derive(shared)


def encrypt_result(result: dict, recipient_public_key_b64: str, *, aad: bytes) -> dict[str, str]:
    sender = X25519PrivateKey.generate()
    recipient = X25519PublicKey.from_public_bytes(_unb64(recipient_public_key_b64))
    key = _result_key(sender, recipient, aad=aad)
    nonce = __import__("secrets").token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, _canonical(result), aad)
    return {
        "sender_public_key": _b64(sender.public_key().public_bytes_raw()),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }


def decrypt_result(
    envelope: dict[str, str],
    recipient_private_key_b64: str,
    *,
    aad: bytes,
) -> dict:
    private = X25519PrivateKey.from_private_bytes(_unb64(recipient_private_key_b64))
    sender = X25519PublicKey.from_public_bytes(_unb64(envelope["sender_public_key"]))
    key = _result_key(private, sender, aad=aad)
    plaintext = AESGCM(key).decrypt(_unb64(envelope["nonce"]), _unb64(envelope["ciphertext"]), aad)
    value = json.loads(plaintext)
    if not isinstance(value, dict):
        raise ValueError("invalid encrypted result")
    return value

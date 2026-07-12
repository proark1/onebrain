"""Ed25519 sign/verify with base64 raw keys. The ONLY module that touches the
signature algorithm — swapping to cosign/Notation later (architecture §10.4)
replaces these internals, not the callers. Private keys never exist on Mission
Control: sign_payload is used exclusively by the offline CLI."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def generate_keypair() -> tuple[str, str]:
    """(private_key_b64, public_key_b64) — 32-byte raw keys, base64."""
    private = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(private.private_bytes_raw()).decode("ascii")
    public_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    return private_b64, public_b64


def public_key_from_private(private_key_b64: str) -> str:
    """Derive the base64 raw public key from a base64 raw Ed25519 private key.
    Used by the desired-state rotation interlock (G1-1) to check that the key MC
    is signing with is in the set delivered to boxes. Raises on a malformed key;
    callers gate on emptiness first (an unset wrapper key means nothing to derive)."""
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")


def sign_payload(payload: bytes, private_key_b64: str) -> str:
    """base64 signature. OFFLINE USE ONLY (scripts/sign_release.py)."""
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(key.sign(payload)).decode("ascii")


def verify_payload(payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """True iff the signature verifies. Never raises: malformed key/signature/
    base64 returns False (fail-closed for callers)."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), payload)
        return True
    except Exception:
        return False

"""Fleet heartbeat key tokens.

Format `fk_<key_id>_<secret>` — the same opaque-bearer, hash-only-stored scheme
as service keys (reusing hash_secret/verify_secret), but a distinct prefix and
store so the two can never be confused. A fleet key only authorizes posting a
heartbeat for its own deployment; it grants no data-plane access.
"""

from __future__ import annotations

import secrets
from typing import Optional

from app.servicekeys.base import hash_secret, verify_secret  # noqa: F401 (re-exported)


def generate_fleet_key() -> tuple[str, str, str]:
    """Return (key_id, secret, plaintext_token). Persist only hash_secret(secret)."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return key_id, secret, f"fk_{key_id}_{secret}"


def parse_fleet_key(token: str) -> Optional[tuple[str, str]]:
    """Split `fk_<key_id>_<secret>` into (key_id, secret), or None if malformed."""
    if not token or not token.startswith("fk_"):
        return None
    key_id, sep, secret = token[3:].partition("_")
    if not sep or not key_id or not secret:
        return None
    return key_id, secret

"""Fleet enrollment key tokens.

Two token families, both the same opaque-bearer, hash-only-stored scheme as
service keys (reusing hash_secret/verify_secret), each with a distinct prefix and
store so they can never be confused:

- `fk_<key_id>_<secret>` — the long-lived, deployment-pinned fleet key. It
  authorizes posting a heartbeat for its own deployment AND (G1-5, Phase 5) — once
  a `box_secret_bundle` exists for that deployment — fetching THAT box's own secret
  bundle via `POST /api/fleet/bootstrap` (the rotation re-fetch channel). It grants
  no cross-deployment access and no MC data-plane access; the elevation is bounded
  to the box's own bundle and rate-limited by a dedicated low bootstrap budget.
- `bt_<token_id>_<secret>` — the single-use, short-TTL FIRST-BOOT bootstrap token
  (P5-03). Consumed atomically as the last step of a successful bundle delivery so a
  lost response never bricks the box (G1-2/G1-8).
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


def generate_bootstrap_token() -> tuple[str, str, str]:
    """Return (token_id, secret, plaintext_token) for a first-boot bootstrap token.
    Persist only hash_secret(secret). Grammar `bt_<token_id>_<secret>` mirrors the
    fleet-key/service-key scheme with a distinct prefix so the two never confuse."""
    token_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return token_id, secret, f"bt_{token_id}_{secret}"


def parse_bootstrap_token(token: str) -> Optional[tuple[str, str]]:
    """Split `bt_<token_id>_<secret>` into (token_id, secret), or None if malformed."""
    if not token or not token.startswith("bt_"):
        return None
    token_id, sep, secret = token[3:].partition("_")
    if not sep or not token_id or not secret:
        return None
    return token_id, secret

"""Signed, expiring session tokens (HMAC-SHA256).

Format: `<b64url payload>.<hex signature>`. The payload holds the user id, an
optional server-side session id, and an expiry. The signature is verified in
constant time before the payload is trusted.

The signature makes a token tamper-evident and self-expiring, but a signed token
alone cannot be revoked before it expires. Session tokens (`make_session_token`)
additionally carry a `sid` that points at a server-side session row, so a login
can be revoked immediately — see app/sessions and resolve_principal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _encode(payload: dict, secret: str, ttl_seconds: int) -> str:
    payload = {**payload, "exp": int(time.time()) + ttl_seconds}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _decode(token: str, secret: str) -> Optional[dict]:
    """Return the payload if the token is well-formed, correctly signed, and
    unexpired, else None."""
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def make_token(user_id: str, secret: str, ttl_seconds: int) -> str:
    return _encode({"uid": user_id}, secret, ttl_seconds)


def read_token(token: str, secret: str) -> Optional[str]:
    """Return the user id if the token is valid and unexpired, else None."""
    payload = _decode(token, secret)
    return payload.get("uid") if payload else None


def make_session_token(user_id: str, session_id: str, secret: str, ttl_seconds: int) -> str:
    """A token bound to a server-side session row, so it can be revoked early."""
    return _encode({"uid": user_id, "sid": session_id}, secret, ttl_seconds)


def read_session_token(token: str, secret: str) -> Optional[tuple[str, str]]:
    """Return (user_id, session_id) for a valid session token, else None. A token
    with no `sid` (a legacy pre-revocation cookie) is rejected — callers must
    re-authenticate to obtain a revocable session."""
    payload = _decode(token, secret)
    if not payload:
        return None
    user_id, session_id = payload.get("uid"), payload.get("sid")
    if not user_id or not session_id:
        return None
    return user_id, session_id

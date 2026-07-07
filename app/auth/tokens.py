"""Signed, expiring session tokens (HMAC-SHA256 — stateless).

Format: `<b64url payload>.<hex signature>`. The payload holds the user id and an
expiry. The signature is verified in constant time before the payload is trusted.
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


def make_token(user_id: str, secret: str, ttl_seconds: int) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + ttl_seconds}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def read_token(token: str, secret: str) -> Optional[str]:
    """Return the user id if the token is valid and unexpired, else None."""
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload.get("uid")
    except Exception:
        return None

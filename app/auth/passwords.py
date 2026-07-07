"""Password hashing with PBKDF2-HMAC-SHA256 (stdlib, portable — no external dep).

Stored form: `pbkdf2$<iterations>$<b64 salt>$<b64 hash>`. Verification is
constant-time. (PBKDF2 is used rather than scrypt because scrypt isn't available
on every OpenSSL/LibreSSL build.)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITERATIONS = 200_000
_DKLEN = 32


def _derive(password: str, salt: bytes, iterations: int, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _derive(password, salt, _ITERATIONS, _DKLEN)
    return f"pbkdf2${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, b64salt, b64hash = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(b64salt)
        expected = base64.b64decode(b64hash)
        candidate = _derive(password, salt, int(iters), len(expected))
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


# A fixed dummy hash so a login attempt for a non-existent user still does the
# same work — no user-enumeration via response timing.
DUMMY_HASH = hash_password("onebrain-dummy-password")

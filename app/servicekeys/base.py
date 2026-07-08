"""Service keys — how a NON-human caller (a comms adapter, a partner service)
authenticates to the brain.

A key is an opaque bearer string `sk_<key_id>_<secret>`. Only a hash of the
secret is stored; the plaintext is shown once at mint time and never again.
`key_id` is a fast lookup handle; the secret is verified constant-time. Because
the secret is high-entropy random (not a human password) a fast SHA-256 hash is
sufficient — there is nothing to brute-force.

Every key is pinned to ONE tenant and carries a set of scopes. It never conveys a
role or a clearance above PUBLIC — see resolve_service_principal.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import List, Optional, Protocol

# The only scopes a service key may hold.
SCOPE_READ = "read:public"       # query the brain, PUBLIC-ceiled, sources stripped
SCOPE_WRITE = "write:capture"    # push content, clamped to INTERNAL/captured_input
VALID_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE})


@dataclass
class ServiceKey:
    id: str
    key_hash: str
    tenant_id: str
    scopes: tuple
    label: str = ""
    account_id: str = ""
    app_id: str = ""
    space_ids: tuple = ()
    purposes: tuple = ()
    status: str = "active"          # active | revoked
    created_at: str = ""


@dataclass(frozen=True)
class ServiceKeySummary:
    total: int
    active: int = 0
    revoked: int = 0


def generate_key() -> tuple[str, str, str]:
    """Return (key_id, secret, plaintext). Persist only hash_secret(secret)."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return key_id, secret, f"sk_{key_id}_{secret}"


def hash_secret(secret: str) -> str:
    return "sha256$" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str, stored: str) -> bool:
    try:
        algo, digest = stored.split("$", 1)
        if algo != "sha256":
            return False
        candidate = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def parse_key(token: str) -> Optional[tuple[str, str]]:
    """Split `sk_<key_id>_<secret>` into (key_id, secret), or None if malformed."""
    if not token or not token.startswith("sk_"):
        return None
    key_id, sep, secret = token[3:].partition("_")
    if not sep or not key_id or not secret:
        return None
    return key_id, secret


class ServiceKeyStore(Protocol):
    def get(self, key_id: str) -> Optional[ServiceKey]: ...

    def create(self, key: ServiceKey) -> ServiceKey: ...

    def list_by_tenant(self, tenant_id: str) -> List[ServiceKey]: ...

    def revoke(self, key_id: str) -> bool: ...

    def summary(self, tenant_id: str = "") -> ServiceKeySummary: ...

"""Server-side login sessions — the revocation layer under the signed cookie.

A signed session token is tamper-evident and self-expiring, but cannot be pulled
before it expires. Each login also creates a `Session` row here; resolve_principal
requires that row to still be present and un-revoked. Offboarding, logout, and a
"log this person out everywhere" action all work by revoking rows here, which
takes effect on the very next request — no waiting for a 7-day token to lapse.

The store keeps no business content: only a session id, the user it belongs to,
the owning tenant (for audit), and lifecycle timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol


@dataclass
class Session:
    id: str
    user_id: str
    tenant_id: str = ""
    created_at: str = ""
    expires_at: str = ""
    revoked_at: str = ""

    @property
    def active(self) -> bool:
        # A live row is one that has not been explicitly revoked. Expiry is a
        # separate axis — see `is_expired` — so this property stays independent
        # of the wall clock.
        return not self.revoked_at

    def is_expired(self, now_iso: str) -> bool:
        # All writers stamp UTC ISO-8601 with a fixed +00:00 offset, so a string
        # comparison orders these chronologically without parsing. An empty
        # expires_at means non-expiring.
        return bool(self.expires_at) and self.expires_at < now_iso


class SessionStore(Protocol):
    def create(self, session: Session) -> Session: ...

    def get(self, session_id: str) -> Optional[Session]: ...

    def revoke(self, session_id: str) -> bool: ...

    def revoke_all_for_user(self, user_id: str) -> int: ...

    def list_for_user(self, user_id: str) -> List[Session]: ...

    def purge_expired(self, now_iso: str) -> int: ...

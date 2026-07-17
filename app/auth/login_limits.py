"""Shared, privacy-preserving login failure counters for API replicas.

The production implementation stores only an HMAC of the account or client
address.  Fixed windows make the counter update a single atomic PostgreSQL
upsert, so every API process sees the same lockout state without Redis.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import time
from datetime import datetime, timezone
from typing import Callable


class PostgresLoginThrottle:
    """Fixed-window login throttle backed by the shared application database."""

    def __init__(
        self,
        dsn: str,
        secret: str,
        max_attempts: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.time,
    ):
        if not dsn.strip():
            raise RuntimeError("ONEBRAIN_DATABASE_URL is required for shared login rate limits.")
        if len(secret) < 32:
            raise RuntimeError("ONEBRAIN_LOGIN_RATE_LIMIT_SECRET must be at least 32 characters.")
        if max_attempts <= 0 or lockout_seconds <= 0:
            raise ValueError("Login throttle limits must be positive.")

        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._secret = secret.encode("utf-8")
        self._max = max_attempts
        self._window = lockout_seconds
        self._clock = clock

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _subject(self, key: str) -> tuple[str, str]:
        scope, separator, value = key.partition(":")
        if not separator or not scope or not value:
            raise ValueError("Login rate-limit keys must be '<scope>:<subject>'.")
        digest = hmac.new(self._secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return scope, digest

    def _window_times(self) -> tuple[datetime, datetime, float]:
        now = self._clock()
        start = math.floor(now / self._window) * self._window
        return (
            datetime.fromtimestamp(start, tz=timezone.utc),
            datetime.fromtimestamp(start + self._window, tz=timezone.utc),
            now,
        )

    def retry_after(self, key: str) -> int:
        scope, subject_hash = self._subject(key)
        window_start, _expires_at, now = self._window_times()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count, expires_at FROM auth_rate_limits "
                "WHERE scope = %s AND subject_hash = %s AND window_started_at = %s",
                (scope, subject_hash, window_start),
            )
            row = cur.fetchone()
        if not row or int(row[0]) < self._max:
            return 0
        expires_at = row[1]
        if not expires_at:
            return 0
        remaining = expires_at.timestamp() - now
        return max(0, math.ceil(remaining))

    def record_failure(self, key: str) -> None:
        self.reserve(key)

    def reserve(self, key: str) -> int:
        """Atomically consume a login-attempt slot and return lockout seconds.

        Admission happens before password verification, so concurrent replicas
        cannot all observe an unused budget and launch an unbounded bcrypt burst.
        A blocked request remains counted for the fixed window.
        """
        scope, subject_hash = self._subject(key)
        window_start, expires_at, now = self._window_times()
        with self._conn() as conn, conn.cursor() as cur:
            # Keep cleanup bounded: it is best-effort hygiene, not part of a
            # caller-controlled full-table operation.
            cur.execute(
                "DELETE FROM auth_rate_limits WHERE ctid IN ("
                "SELECT ctid FROM auth_rate_limits WHERE expires_at <= %s "
                "ORDER BY expires_at LIMIT 100"
                ")",
                (datetime.fromtimestamp(now, tz=timezone.utc),),
            )
            cur.execute(
                "INSERT INTO auth_rate_limits "
                "(scope, subject_hash, window_started_at, attempt_count, expires_at) "
                "VALUES (%s, %s, %s, 1, %s) "
                "ON CONFLICT (scope, subject_hash, window_started_at) DO UPDATE "
                "SET attempt_count = auth_rate_limits.attempt_count + 1, "
                "expires_at = EXCLUDED.expires_at "
                "RETURNING attempt_count, expires_at",
                (scope, subject_hash, window_start, expires_at),
            )
            row = cur.fetchone()
            conn.commit()
        attempt_count = int(row[0]) if row else self._max + 1
        if attempt_count <= self._max:
            return 0
        return max(1, math.ceil(expires_at.timestamp() - now))

    def record_success(self, key: str) -> None:
        scope, subject_hash = self._subject(key)
        with self._conn() as conn, conn.cursor() as cur:
            # A valid credential clears only its account key. Callers must not
            # clear the IP key, otherwise a successful credential could reset an
            # attacker-wide brute-force limit for that address.
            cur.execute(
                "DELETE FROM auth_rate_limits WHERE scope = %s AND subject_hash = %s",
                (scope, subject_hash),
            )
            conn.commit()

    def release_success(self, key: str) -> None:
        """Remove one successful reservation without clearing other failures."""
        scope, subject_hash = self._subject(key)
        window_start, _expires_at, _now = self._window_times()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auth_rate_limits "
                "WHERE scope = %s AND subject_hash = %s AND window_started_at = %s "
                "AND attempt_count <= 1",
                (scope, subject_hash, window_start),
            )
            cur.execute(
                "UPDATE auth_rate_limits SET attempt_count = attempt_count - 1 "
                "WHERE scope = %s AND subject_hash = %s AND window_started_at = %s "
                "AND attempt_count > 1",
                (scope, subject_hash, window_start),
            )
            conn.commit()


class PostgresRateLimiter:
    """Shared fixed-window limiter for replica-visible non-login budgets.

    The hashed-counter table also safely holds service-key and fleet budgets.
    A distinct scope domain-separates each endpoint class; raw caller keys are
    never stored in PostgreSQL.
    """

    def __init__(
        self,
        dsn: str,
        secret: str,
        limit: int,
        window_seconds: int,
        *,
        scope: str,
        clock: Callable[[], float] = time.time,
    ):
        if not dsn.strip():
            raise RuntimeError("ONEBRAIN_DATABASE_URL is required for shared rate limits.")
        if len(secret) < 32:
            raise RuntimeError("ONEBRAIN_LOGIN_RATE_LIMIT_SECRET must be at least 32 characters.")
        if not scope or ":" in scope:
            raise ValueError("Rate-limit scope must be a non-empty simple identifier.")
        if window_seconds <= 0:
            raise ValueError("Rate-limit window must be positive.")

        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._secret = secret.encode("utf-8")
        self._limit = limit
        self._window = window_seconds
        self._scope = scope
        self._clock = clock

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _subject_hash(self, key: str) -> str:
        if not key:
            raise ValueError("Rate-limit key must be non-empty.")
        return hmac.new(self._secret, key.encode("utf-8"), hashlib.sha256).hexdigest()

    def check(self, key: str) -> int:
        """Consume one shared budget unit and return retry seconds when blocked."""
        subject_hash = self._subject_hash(key)
        now = self._clock()
        start = math.floor(now / self._window) * self._window
        window_started_at = datetime.fromtimestamp(start, tz=timezone.utc)
        expires_at = datetime.fromtimestamp(start + self._window, tz=timezone.utc)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auth_rate_limits WHERE ctid IN ("
                "SELECT ctid FROM auth_rate_limits WHERE expires_at <= %s "
                "ORDER BY expires_at LIMIT 100"
                ")",
                (datetime.fromtimestamp(now, tz=timezone.utc),),
            )
            cur.execute(
                "INSERT INTO auth_rate_limits "
                "(scope, subject_hash, window_started_at, attempt_count, expires_at) "
                "VALUES (%s, %s, %s, 1, %s) "
                "ON CONFLICT (scope, subject_hash, window_started_at) DO UPDATE "
                "SET attempt_count = auth_rate_limits.attempt_count + 1, "
                "expires_at = EXCLUDED.expires_at "
                "RETURNING attempt_count, expires_at",
                (self._scope, subject_hash, window_started_at, expires_at),
            )
            row = cur.fetchone()
            conn.commit()
        attempt_count = int(row[0]) if row else self._limit + 1
        if attempt_count <= self._limit:
            return 0
        return max(1, math.ceil(expires_at.timestamp() - now))


def client_ip_from_request(request, *, trusted_proxy_cidrs: str, trusted_proxy_hops: int) -> str:
    """Return a safely-derived client address without trusting arbitrary headers.

    By default the ASGI peer is authoritative.  Forwarded addresses are accepted
    only when that peer is in an explicitly configured proxy CIDR and the caller
    has declared how many trusted proxies append to ``X-Forwarded-For``.  A bad
    header or configuration falls back to the direct peer rather than allowing a
    client to select its own rate-limit bucket.
    """
    peer = str(getattr(getattr(request, "client", None), "host", "") or "unknown").strip()
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"

    if trusted_proxy_hops <= 0 or not trusted_proxy_cidrs.strip():
        return peer
    try:
        trusted_networks = [
            ipaddress.ip_network(value.strip(), strict=False)
            for value in trusted_proxy_cidrs.split(",")
            if value.strip()
        ]
    except ValueError:
        return peer
    if not trusted_networks or not any(peer_address in network for network in trusted_networks):
        return peer

    headers = getattr(request, "headers", {}) or {}
    forwarded = headers.get("x-forwarded-for", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) < trusted_proxy_hops:
        return peer

    # For N trusted proxies, the Nth address from the right is the client and
    # the N-1 addresses after it must be trusted intermediary proxies.
    intermediary = parts[-(trusted_proxy_hops - 1):] if trusted_proxy_hops > 1 else []
    try:
        intermediary_addresses = [ipaddress.ip_address(value) for value in intermediary]
        candidate = ipaddress.ip_address(parts[-trusted_proxy_hops])
    except ValueError:
        return peer
    if not all(any(address in network for network in trusted_networks) for address in intermediary_addresses):
        return peer
    return str(candidate)

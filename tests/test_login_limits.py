from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.auth.login_limits import PostgresLoginThrottle, PostgresRateLimiter
from app.auth.login_limits import client_ip_from_request


class Clock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeCursor:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.cursor_value = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1


class FakePsycopg:
    def __init__(self, connection: FakeConnection):
        self.connection = connection
        self.dsns: list[str] = []

    def connect(self, dsn):
        self.dsns.append(dsn)
        return self.connection


def _throttle(monkeypatch, *, clock: Clock | None = None):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    fake_psycopg = FakePsycopg(connection)
    # Constructor imports lazily; pre-seed the module cache with a compatible
    # object so these unit tests never need a running Postgres service.
    import sys

    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    throttle = PostgresLoginThrottle(
        "postgresql://unit-test", "s" * 32, 3, 60, clock=clock or Clock()
    )
    return throttle, cursor, connection, fake_psycopg


def test_postgres_throttle_hashes_subject_and_uses_atomic_upsert(monkeypatch):
    throttle, cursor, connection, _ = _throttle(monkeypatch)

    throttle.record_failure("email:Owner@Example.test")

    assert connection.commits == 1
    upsert_sql, params = cursor.calls[-1]
    assert "ON CONFLICT (scope, subject_hash, window_started_at) DO UPDATE" in upsert_sql
    assert params[0] == "email"
    assert params[1] != "Owner@Example.test"
    assert len(params[1]) == 64
    assert "Owner@Example.test" not in repr(cursor.calls)


def test_postgres_throttle_returns_shared_window_retry_after(monkeypatch):
    clock = Clock(1_000.0)
    throttle, cursor, _, _ = _throttle(monkeypatch, clock=clock)
    cursor.row = (3, datetime.fromtimestamp(1_020, tz=timezone.utc))

    assert throttle.retry_after("ip:203.0.113.9") == 20


def test_postgres_throttle_allows_unlocked_or_expired_rows(monkeypatch):
    clock = Clock(1_000.0)
    throttle, cursor, _, _ = _throttle(monkeypatch, clock=clock)
    cursor.row = (2, datetime.fromtimestamp(1_020, tz=timezone.utc))
    assert throttle.retry_after("ip:203.0.113.9") == 0
    cursor.row = (3, datetime.fromtimestamp(999, tz=timezone.utc))
    assert throttle.retry_after("ip:203.0.113.9") == 0


def test_postgres_throttle_clears_only_hashed_subject(monkeypatch):
    throttle, cursor, connection, _ = _throttle(monkeypatch)

    throttle.record_success("email:owner@example.test")

    assert connection.commits == 1
    sql, params = cursor.calls[-1]
    assert "DELETE FROM auth_rate_limits" in sql
    assert params[0] == "email" and params[1] != "owner@example.test"


def test_postgres_throttle_rejects_bad_secret_or_key(monkeypatch):
    with pytest.raises(RuntimeError, match="LOGIN_RATE_LIMIT_SECRET"):
        PostgresLoginThrottle("postgresql://unit-test", "short", 3, 60)

    throttle, _, _, _ = _throttle(monkeypatch)
    with pytest.raises(ValueError, match="scope"):
        throttle.retry_after("not-a-scoped-key")


def test_postgres_rate_limiter_uses_atomic_shared_counter(monkeypatch):
    clock = Clock(1_000.0)
    cursor = FakeCursor()
    cursor.row = (3, datetime.fromtimestamp(1_020, tz=timezone.utc))
    connection = FakeConnection(cursor)
    fake_psycopg = FakePsycopg(connection)
    import sys

    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    limiter = PostgresRateLimiter(
        "postgresql://unit-test", "s" * 32, 2, 60, scope="service_key", clock=clock
    )

    assert limiter.check("svc:key") == 20
    assert connection.commits == 1
    upsert_sql, params = cursor.calls[-1]
    assert "ON CONFLICT (scope, subject_hash, window_started_at) DO UPDATE" in upsert_sql
    assert "RETURNING attempt_count, expires_at" in upsert_sql
    assert params[0] == "service_key"
    assert params[1] != "svc:key" and len(params[1]) == 64


def _request(peer: str, forwarded: str = ""):
    return type(
        "Request",
        (),
        {"client": type("Client", (), {"host": peer})(), "headers": {"x-forwarded-for": forwarded}},
    )()


def test_client_ip_ignores_untrusted_forwarded_headers():
    request = _request("198.51.100.9", "203.0.113.7")

    assert client_ip_from_request(
        request, trusted_proxy_cidrs="10.0.0.0/8", trusted_proxy_hops=1
    ) == "198.51.100.9"


def test_client_ip_uses_forwarded_client_only_through_configured_proxy():
    request = _request("10.1.2.3", "203.0.113.7")

    assert client_ip_from_request(
        request, trusted_proxy_cidrs="10.0.0.0/8", trusted_proxy_hops=1
    ) == "203.0.113.7"


def test_client_ip_validates_each_declared_proxy_hop():
    request = _request("10.1.2.3", "203.0.113.7, 10.2.3.4")
    assert client_ip_from_request(
        request, trusted_proxy_cidrs="10.0.0.0/8", trusted_proxy_hops=2
    ) == "203.0.113.7"

    untrusted_chain = _request("10.1.2.3", "203.0.113.7, 198.51.100.9")
    assert client_ip_from_request(
        untrusted_chain, trusted_proxy_cidrs="10.0.0.0/8", trusted_proxy_hops=2
    ) == "10.1.2.3"

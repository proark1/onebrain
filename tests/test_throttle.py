"""Login throttle: lock out after N failures, clear on success, expire after the
cooldown. A controllable clock keeps the test fast and deterministic.
"""

from __future__ import annotations

from app.auth.throttle import LoginThrottle


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_locks_out_after_max_attempts():
    clk = _Clock()
    th = LoginThrottle(max_attempts=3, lockout_seconds=900, clock=clk)
    assert th.retry_after("k") == 0
    th.record_failure("k")
    th.record_failure("k")
    assert th.retry_after("k") == 0                 # 2 < 3, still open
    th.record_failure("k")
    assert th.retry_after("k") > 0                  # 3 -> locked


def test_success_clears_failures():
    th = LoginThrottle(3, 900)
    for _ in range(3):
        th.record_failure("k")
    assert th.retry_after("k") > 0
    th.record_success("k")
    assert th.retry_after("k") == 0


def test_lockout_expires_after_cooldown():
    clk = _Clock()
    th = LoginThrottle(3, 900, clock=clk)
    for _ in range(3):
        th.record_failure("k")
    assert th.retry_after("k") > 0
    clk.advance(901)
    assert th.retry_after("k") == 0                 # failure window has passed


def test_keys_are_independent():
    th = LoginThrottle(3, 900)
    for _ in range(3):
        th.record_failure("email:a@x.de")
    assert th.retry_after("email:a@x.de") > 0
    assert th.retry_after("email:b@x.de") == 0      # a different account is unaffected

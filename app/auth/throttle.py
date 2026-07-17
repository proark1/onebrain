"""Login throttle — lock out brute-force / credential-stuffing on a per-account
basis after too many recent failures.

In-memory and per-process: correct and dependency-free for a single instance. A
multi-replica deployment would move this state to Redis (and add per-IP / global
limits at the edge/WAF) — see the infra phase of the plan.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List


class LoginThrottle:
    def __init__(self, max_attempts: int, lockout_seconds: int, clock: Callable[[], float] = time.time):
        self._max = max_attempts
        self._cooldown = lockout_seconds
        self._clock = clock
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> List[float]:
        now = self._clock()
        times = [t for t in self._fails.get(key, []) if now - t < self._cooldown]
        if times:
            self._fails[key] = times
        else:
            self._fails.pop(key, None)
        return times

    def retry_after(self, key: str) -> int:
        """Seconds the key must wait before another attempt, or 0 if not locked."""
        with self._lock:
            times = self._recent(key)
            if len(times) >= self._max:
                wait = self._cooldown - (self._clock() - min(times))
                return max(1, int(wait) + 1)
            return 0

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(self._clock())

    def reserve(self, key: str) -> int:
        """Consume an attempt slot before a password hash is evaluated."""
        with self._lock:
            times = self._recent(key)
            if len(times) >= self._max:
                wait = self._cooldown - (self._clock() - min(times))
                return max(1, int(wait) + 1)
            times.append(self._clock())
            self._fails[key] = times
            return 0

    def record_success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def release_success(self, key: str) -> None:
        """Undo one successful reservation without clearing other failures."""
        with self._lock:
            times = self._recent(key)
            if times:
                times.pop()
            if times:
                self._fails[key] = times
            else:
                self._fails.pop(key, None)


class RateLimiter:
    """Sliding-window rate limit: at most `limit` calls per `window` seconds per
    key. In-memory/per-process (fine for a single instance; move to Redis for
    multi-replica). Used to cap metered service-key endpoints so a leaked key
    cannot be looped for unbounded LLM/embedding cost."""

    def __init__(self, limit: int, window_seconds: int, clock: Callable[[], float] = time.time):
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int:
        """Record a call and return 0 if allowed, else seconds to wait (429)."""
        with self._lock:
            now = self._clock()
            times = [t for t in self._events.get(key, []) if now - t < self._window]
            if len(times) >= self._limit:
                self._events[key] = times
                return max(1, int(self._window - (now - min(times))) + 1)
            times.append(now)
            self._events[key] = times
            return 0

"""In-process operational counters for the operator cockpit.

These counters are intentionally coarse and content-free. They give the admin
UI an immediate signal for auth failures and API errors without persisting user
input, secrets, or request payloads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MonitoringSummary:
    auth_failures: dict[str, int] = field(default_factory=dict)
    auth_total: int = 0
    login_failures: int = 0
    service_key_failures: int = 0
    lockouts: int = 0
    last_auth_failure_at: str = ""
    api_errors_5xx: int = 0
    last_api_error_at: str = ""
    last_api_error_route: str = ""
    last_api_error_status: int = 0


class MonitoringMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._auth_failures: dict[str, int] = {}
        self._last_auth_failure_at = ""
        self._api_errors_5xx = 0
        self._last_api_error_at = ""
        self._last_api_error_route = ""
        self._last_api_error_status = 0

    def record_auth_failure(self, kind: str) -> None:
        clean = _clean_label(kind, "auth_failure")
        with self._lock:
            self._auth_failures[clean] = self._auth_failures.get(clean, 0) + 1
            self._last_auth_failure_at = _now()

    def record_api_error(self, *, route: str, status_code: int) -> None:
        with self._lock:
            self._api_errors_5xx += 1
            self._last_api_error_at = _now()
            self._last_api_error_route = _clean_route(route)
            self._last_api_error_status = status_code

    def snapshot(self) -> MonitoringSummary:
        with self._lock:
            auth_failures = dict(self._auth_failures)
            auth_total = sum(auth_failures.values())
            service_key_failures = sum(
                count for kind, count in auth_failures.items()
                if kind.startswith("service_key")
            )
            return MonitoringSummary(
                auth_failures=auth_failures,
                auth_total=auth_total,
                login_failures=auth_failures.get("login_invalid", 0),
                service_key_failures=service_key_failures,
                lockouts=auth_failures.get("login_locked", 0),
                last_auth_failure_at=self._last_auth_failure_at,
                api_errors_5xx=self._api_errors_5xx,
                last_api_error_at=self._last_api_error_at,
                last_api_error_route=self._last_api_error_route,
                last_api_error_status=self._last_api_error_status,
            )

    def reset(self) -> None:
        with self._lock:
            self._auth_failures = {}
            self._last_auth_failure_at = ""
            self._api_errors_5xx = 0
            self._last_api_error_at = ""
            self._last_api_error_route = ""
            self._last_api_error_status = 0


def _clean_label(value: str, fallback: str) -> str:
    clean = "".join(ch for ch in (value or "") if ch.isalnum() or ch in "._:-")
    return (clean or fallback)[:80]


def _clean_route(value: str) -> str:
    clean = "".join(ch for ch in (value or "") if ch.isalnum() or ch in "/{}._:-")
    return (clean or "unknown")[:160]


_METRICS = MonitoringMetrics()


def record_auth_failure(kind: str) -> None:
    _METRICS.record_auth_failure(kind)


def record_api_error(*, route: str, status_code: int) -> None:
    _METRICS.record_api_error(route=route, status_code=status_code)


def monitoring_snapshot() -> MonitoringSummary:
    return _METRICS.snapshot()


def reset_monitoring_metrics() -> None:
    _METRICS.reset()

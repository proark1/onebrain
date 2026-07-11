"""The watcher: turn heartbeat state into open/resolved alerts.

`desired_alerts` is pure (no store) so the alerting rules are unit-testable;
`run_watchdog` reconciles that desire against the store — opening alerts that
should exist and resolving ones that no longer apply. A separate external
dead-man ping (e.g. UptimeRobot on Mission Control's own /health) is the
watcher-of-the-watcher, since a dead Mission Control cannot alert on itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from app.fleet.base import FleetAlert, FleetStore, Heartbeat


def _age_seconds(reference_iso: str, now_iso: str) -> Optional[float]:
    try:
        return (datetime.fromisoformat(now_iso) - datetime.fromisoformat(reference_iso)).total_seconds()
    except (ValueError, TypeError):
        return None


def desired_alerts(
    *,
    heartbeat: Optional[Heartbeat],
    now_iso: str,
    missed_after_seconds: float,
    expected_version: str = "",
) -> Dict[str, str]:
    """Which alert kinds SHOULD be open for one deployment, kind -> detail."""
    alerts: Dict[str, str] = {}
    if heartbeat is None:
        alerts["missed_heartbeat"] = "no heartbeat received yet"
        return alerts

    age = _age_seconds(heartbeat.received_at, now_iso)
    if age is not None and age > missed_after_seconds:
        alerts["missed_heartbeat"] = f"last heartbeat {int(age)}s ago (threshold {int(missed_after_seconds)}s)"

    # A deployment reporting unhealthy is only alertable while it is still
    # reporting — if it has also gone silent, the missed-heartbeat alert leads.
    if "missed_heartbeat" not in alerts and not heartbeat.healthy:
        alerts["unhealthy"] = "deployment reported unhealthy"

    if expected_version and heartbeat.version and heartbeat.version != expected_version:
        alerts["version_drift"] = f"running {heartbeat.version}, fleet target {expected_version}"

    return alerts


def run_watchdog(
    store: FleetStore,
    deployment_ids: List[str],
    *,
    now_iso: str,
    missed_after_seconds: float,
    expected_version: str = "",
    next_id,
) -> List[FleetAlert]:
    """Reconcile alerts for every deployment. Returns the alerts opened this pass.
    `next_id` is a callable returning a fresh alert id (kept out so callers control
    id generation / determinism in tests)."""
    latest = store.latest_heartbeats()
    opened: List[FleetAlert] = []
    for deployment_id in deployment_ids:
        want = desired_alerts(
            heartbeat=latest.get(deployment_id),
            now_iso=now_iso,
            missed_after_seconds=missed_after_seconds,
            expected_version=expected_version,
        )
        # Open any wanted alert that is not already open.
        for kind, detail in want.items():
            if not store.has_open_alert(deployment_id, kind):
                opened.append(store.open_alert(FleetAlert(
                    id=next_id(), deployment_id=deployment_id, kind=kind,
                    detail=detail, status="open", created_at=now_iso,
                )))
        # Resolve any open alert that is no longer wanted.
        for existing in store.list_open_alerts(deployment_id):
            if existing.kind not in want:
                store.resolve_open_alerts(deployment_id, existing.kind, now_iso)
    return opened

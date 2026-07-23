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

# The alert kinds THIS watchdog opens from heartbeat state. It must resolve only these —
# other kinds in the ledger (e.g. the pipeline watchdog's control-plane alerts, which can
# live on the same Mission Control deployment row) are not ours to clear.
WATCHDOG_ALERT_KINDS = frozenset({
    "missed_heartbeat", "unhealthy", "version_drift",
    "low_root_disk", "low_data_disk", "data_volume_unavailable",
})


def _age_seconds(reference_iso: str, now_iso: str) -> Optional[float]:
    try:
        return (datetime.fromisoformat(now_iso) - datetime.fromisoformat(reference_iso)).total_seconds()
    except (ValueError, TypeError):
        return None


def _free_percent(heartbeat: Optional[Heartbeat], volume: str) -> Optional[float]:
    """Return a reported volume's usable-space percentage, if it is trustworthy.

    Heartbeats are validated at ingest, but this defensive reader also handles
    old persisted payloads and manually constructed test records. Unknown
    capacity (``0/0``) remains unknown rather than being treated as a full disk.
    """
    if heartbeat is None:
        return None
    payload = heartbeat.payload if isinstance(heartbeat.payload, dict) else {}
    storage = payload.get("storage")
    if not isinstance(storage, dict):
        return None
    capacity = storage.get(volume)
    if not isinstance(capacity, dict):
        return None
    total = capacity.get("total_bytes")
    available = capacity.get("available_bytes")
    if (
        isinstance(total, bool)
        or isinstance(available, bool)
        or not isinstance(total, int)
        or not isinstance(available, int)
        or total <= 0
        or available < 0
        or available > total
    ):
        return None
    return available * 100 / total


def _data_volume_unavailable(heartbeat: Optional[Heartbeat]) -> Optional[bool]:
    """Return the explicit host verification signal, preserving legacy unknown."""
    if heartbeat is None:
        return None
    payload = heartbeat.payload if isinstance(heartbeat.payload, dict) else {}
    storage = payload.get("storage")
    if not isinstance(storage, dict):
        return None
    value = storage.get("data_volume_unavailable")
    return value if isinstance(value, bool) else None


def _low_disk_detail(heartbeat: Heartbeat, *, volume: str, threshold_percent: float) -> Optional[str]:
    """Human-readable metadata-only alert detail, or ``None`` when healthy/unknown."""
    if threshold_percent <= 0:
        return None
    free_percent = _free_percent(heartbeat, volume)
    if free_percent is None or free_percent > threshold_percent:
        return None
    label = "root" if volume == "root" else "data"
    return f"{label} disk has {free_percent:.1f}% free (threshold {threshold_percent:g}%)"


def desired_alerts(
    *,
    heartbeat: Optional[Heartbeat],
    now_iso: str,
    missed_after_seconds: float,
    expected_version: str = "",
    low_root_disk_percent: float = 0,
    low_data_disk_percent: float = 0,
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

    # Preserve a last-known low-disk signal even if the box later goes silent:
    # resolving it solely because telemetry stopped would give a misleading all
    # clear. A new capacity report above the threshold resolves it normally.
    root_detail = _low_disk_detail(
        heartbeat, volume="root", threshold_percent=low_root_disk_percent,
    )
    if root_detail:
        alerts["low_root_disk"] = root_detail
    data_detail = _low_disk_detail(
        heartbeat, volume="data", threshold_percent=low_data_disk_percent,
    )
    if data_detail:
        alerts["low_data_disk"] = data_detail
    if _data_volume_unavailable(heartbeat) is True:
        alerts["data_volume_unavailable"] = "persistent data volume is unavailable or failed verification"

    return alerts


def run_watchdog(
    store: FleetStore,
    deployment_ids: List[str],
    *,
    now_iso: str,
    missed_after_seconds: float,
    expected_version: str = "",
    low_root_disk_percent: float = 0,
    low_data_disk_percent: float = 0,
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
            low_root_disk_percent=low_root_disk_percent,
            low_data_disk_percent=low_data_disk_percent,
        )
        # Open any wanted alert that is not already open.
        for kind, detail in want.items():
            if not store.has_open_alert(deployment_id, kind):
                opened.append(store.open_alert(FleetAlert(
                    id=next_id(), deployment_id=deployment_id, kind=kind,
                    detail=detail, status="open", created_at=now_iso,
                )))
        # Resolve any open alert that is no longer wanted.  Disk telemetry is
        # deliberately asymmetric: a missing/unknown capacity cannot prove a
        # previously low disk recovered.  Keep the last-known low-disk signal
        # open until a *known* healthy capacity arrives (or its threshold is
        # intentionally disabled).
        for existing in store.list_open_alerts(deployment_id):
            if existing.kind not in WATCHDOG_ALERT_KINDS:
                continue  # not a heartbeat alert — leave pipeline/other kinds untouched
            if existing.kind not in want:
                if existing.kind == "low_root_disk" and low_root_disk_percent > 0:
                    if _free_percent(latest.get(deployment_id), "root") is None:
                        continue
                if existing.kind == "low_data_disk" and low_data_disk_percent > 0:
                    if _free_percent(latest.get(deployment_id), "data") is None:
                        continue
                if existing.kind == "data_volume_unavailable":
                    # Missing on older reporters is unknown, not proof that the
                    # verified mount recovered. Only an explicit false resolves.
                    if _data_volume_unavailable(latest.get(deployment_id)) is None:
                        continue
                store.resolve_open_alerts(deployment_id, existing.kind, now_iso)
    return opened

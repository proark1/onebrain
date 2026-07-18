"""Mission Control scheduler for fleet health and storage alerts.

The watchdog is deliberately separate from rollout reconciliation: it only
opens/resolves alert-ledger rows and never changes a deployment's desired state.
One tick is kept side-effect-light and directly testable; the daemon wrapper
matches the existing fleet reporter and retention timers.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.fleet.watchdog import run_watchdog

_log = logging.getLogger("onebrain.fleet")


def watchdog_once(settings, control_store, fleet_store) -> list:
    """Reconcile heartbeat, version, and capacity alerts for every deployment."""
    deployment_ids = [deployment.id for deployment in control_store.list_deployments()]
    return run_watchdog(
        fleet_store,
        deployment_ids,
        now_iso=datetime.now(timezone.utc).isoformat(),
        missed_after_seconds=max(1, float(settings.fleet_missed_heartbeat_seconds)),
        expected_version=str(getattr(settings, "fleet_target_version", "") or ""),
        low_root_disk_percent=float(getattr(settings, "fleet_low_root_disk_percent", 0) or 0),
        low_data_disk_percent=float(getattr(settings, "fleet_low_data_disk_percent", 0) or 0),
        next_id=lambda: f"fa_{uuid4().hex}",
    )


def start_fleet_watchdog(settings) -> bool:
    """Start the Mission Control alert scheduler, unless explicitly disabled.

    A positive interval is clamped to 30 seconds so an environment typo cannot
    spin the datastore. Each failed tick is isolated: heartbeat ingest and the
    operator UI continue serving during a transient datastore failure.
    """
    interval_setting = int(getattr(settings, "fleet_watchdog_seconds", 0) or 0)
    if not getattr(settings, "operator_mode", False) or interval_setting <= 0:
        return False
    interval = max(30, interval_setting)

    def _loop() -> None:
        from app.deps import get_control_plane_store, get_fleet_store

        while True:
            try:
                watchdog_once(settings, get_control_plane_store(), get_fleet_store())
            except Exception as exc:  # pragma: no cover - defensive daemon boundary
                _log.warning("Fleet watchdog tick failed: %s", exc)
            time.sleep(interval)

    threading.Thread(target=_loop, name="fleet-watchdog", daemon=True).start()
    _log.info("Fleet watchdog started (every %ss).", interval)
    return True

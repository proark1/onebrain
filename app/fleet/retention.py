"""Fleet heartbeat retention — Mission Control prunes old heartbeats on a timer so
the append-only fleet_heartbeats table stays bounded.

`prune_once` computes the cutoff and calls the store (pure of scheduling, so it is
unit-testable). `start_fleet_retention` runs it daily on a daemon thread, only on a
Mission Control deployment (operator_mode).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

_log = logging.getLogger("onebrain.fleet")


def prune_once(settings, fleet_store) -> int:
    days = max(1, int(settings.fleet_heartbeat_retention_days))
    before_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return fleet_store.prune_heartbeats(before_iso)


def start_fleet_retention(settings) -> bool:
    """Daemon: prune fleet_heartbeats older than the retention window, daily. Only on
    Mission Control. Never fatal — a prune failure must not disturb ingest/serving."""
    if not settings.operator_mode:
        return False

    def _loop() -> None:
        from app.deps import get_fleet_store

        while True:
            try:
                removed = prune_once(settings, get_fleet_store())
                if removed:
                    _log.info("Pruned %s heartbeats older than %s days.", removed, settings.fleet_heartbeat_retention_days)
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning("Fleet heartbeat prune failed: %s", exc)
            time.sleep(24 * 3600)

    threading.Thread(target=_loop, name="fleet-retention", daemon=True).start()
    _log.info("Fleet heartbeat retention started (%s-day window).", settings.fleet_heartbeat_retention_days)
    return True

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

from app.fleet.pipeline_watchdog import run_pipeline_watchdog
from app.fleet.watchdog import run_watchdog

_log = logging.getLogger("onebrain.fleet")


def watchdog_once(settings, control_store, fleet_store) -> list:
    """Reconcile heartbeat, version, and capacity alerts for every deployment, plus the
    release-pipeline stall alerts on Mission Control's own row (roadmap Gap D)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    deployment_ids = [deployment.id for deployment in control_store.list_deployments()]
    opened = run_watchdog(
        fleet_store,
        deployment_ids,
        now_iso=now_iso,
        missed_after_seconds=max(1, float(settings.fleet_missed_heartbeat_seconds)),
        expected_version=str(getattr(settings, "fleet_target_version", "") or ""),
        low_root_disk_percent=float(getattr(settings, "fleet_low_root_disk_percent", 0) or 0),
        low_data_disk_percent=float(getattr(settings, "fleet_low_data_disk_percent", 0) or 0),
        next_id=lambda: f"fa_{uuid4().hex}",
    )
    opened += _run_pipeline_watchdog(settings, control_store, fleet_store, now_iso)
    _push_alert_webhook(settings, opened)
    return opened


def _push_alert_webhook(settings, opened) -> None:
    """Deliver newly-opened alerts (infra + pipeline) to the configured webhook (roadmap
    Gap D). Dormant until fleet_alert_webhook_url is set; push_open_alerts never raises."""
    url = (getattr(settings, "fleet_alert_webhook_url", "") or "").strip()
    if not url or not opened:
        return
    from app.fleet.alert_notify import push_open_alerts

    push_open_alerts(url, opened)


def _run_pipeline_watchdog(settings, control_store, fleet_store, now_iso: str) -> list:
    """Mission-Control-only release-pipeline stall alerts (roadmap Gap D). Never raises: a
    control-store hiccup degrades to no pipeline alerts this tick, never a crashed watchdog."""
    if not getattr(settings, "operator_mode", False):
        return []
    mc_deployment_id = (getattr(settings, "deployment_id", "") or "").strip()
    if not mc_deployment_id:
        return []
    try:
        return run_pipeline_watchdog(
            control_store, fleet_store,
            now_iso=now_iso, mc_deployment_id=mc_deployment_id,
            stall_seconds=int(getattr(settings, "pipeline_stall_alert_seconds", 0) or 0),
            self_deploy_enabled=bool(getattr(settings, "operator_auto_deploy_enabled", False)),
            # Default 1 = MC's give-up budget BEFORE the bounded self-deploy retry (#63) lands:
            # today dispatch_operator_self_rollout stops after the first failed rollout, so the
            # "gave up" signal is 1 failure. Once #63 sets operator_self_max_attempts (default 3),
            # this getattr picks it up and the alert threshold tracks the real budget. Defaulting
            # to 3 here would leave the alert silent on the current single-attempt behavior.
            self_max_attempts=int(getattr(settings, "operator_self_max_attempts", 1) or 1),
            next_id=lambda: f"fa_{uuid4().hex}",
        )
    except Exception as exc:  # never let pipeline detection break the watchdog daemon
        _log.warning("Pipeline watchdog tick failed: %s", exc)
        return []


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

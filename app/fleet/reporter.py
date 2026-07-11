"""The reporter: a deployment posts its own metadata-only heartbeat to Mission
Control on a timer.

`collect_heartbeat` builds the fleet.v1 body from this deployment's own
observability counts — pure of network, so it is unit-testable. `send_heartbeat`
does the one POST (stdlib urllib, no new dependency; the opener is injectable for
tests). `report_once` glues them and NEVER raises — a reporting failure must not
disturb the serving deployment. `start_reporter` runs it on a daemon timer.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone

from app.config import Settings
from app.db.schema import REQUIRED_ALEMBIC_REVISION
from app.fleet.heartbeat import FleetHeartbeat, build_heartbeat

_log = logging.getLogger("onebrain.fleet")


def collect_heartbeat(settings: Settings) -> FleetHeartbeat:
    from app.deps import (
        get_intake_store, get_job_store, get_platform_store, get_service_key_store,
        get_store, get_user_store,
    )
    from app.monitoring import monitoring_snapshot

    metrics = monitoring_snapshot()
    jobs = get_job_store().summary(recent_failures_limit=0)
    return build_heartbeat(
        deployment_id=settings.deployment_id,
        reported_at=datetime.now(timezone.utc).isoformat(),
        migration_revision=REQUIRED_ALEMBIC_REVISION,
        onebrain_healthy=True,
        chunks=get_store().count(),
        intake_records=get_intake_store().count(),
        users=get_user_store().count(),
        accounts=len(get_platform_store().list_accounts()),
        active_service_keys=get_service_key_store().summary().active,
        jobs_pending=int(jobs.by_status.get("pending", 0)),
        jobs_failed=int(jobs.by_status.get("failed", 0)),
        auth_failures_recent=int(metrics.auth_total),
        api_5xx_recent=int(metrics.api_errors_5xx),
    )


def send_heartbeat(fleet_url: str, fleet_key: str, heartbeat: FleetHeartbeat, *, opener=None, timeout: float = 10.0) -> int:
    """POST the heartbeat; return the HTTP status. `opener(request, timeout)` is
    injectable so tests need no network."""
    url = fleet_url.rstrip("/") + "/api/fleet/heartbeat"
    data = json.dumps(heartbeat.model_dump()).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {fleet_key}"},
    )
    do_open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))
    with do_open(request, timeout) as response:
        return getattr(response, "status", 0) or response.getcode()


def report_once(settings: Settings, *, opener=None) -> bool:
    """Collect and send one heartbeat. Never raises; returns whether it posted."""
    if not settings.fleet_url or not settings.fleet_key or not settings.deployment_id:
        return False
    try:
        heartbeat = collect_heartbeat(settings)
        status = send_heartbeat(settings.fleet_url, settings.fleet_key, heartbeat, opener=opener)
        if status >= 400:
            _log.warning("Fleet heartbeat rejected with HTTP %s", status)
            return False
        return True
    except Exception as exc:  # a reporting failure must never disturb serving
        _log.warning("Fleet heartbeat failed: %s", exc)
        return False


def start_reporter(settings: Settings) -> bool:
    """Spawn a daemon thread that reports every fleet_report_seconds. Returns
    whether it started (only when this deployment is configured to report)."""
    if not settings.fleet_url or not settings.fleet_key or not settings.deployment_id:
        return False

    interval = max(10, int(settings.fleet_report_seconds))

    def _loop() -> None:
        import time

        while True:
            report_once(settings)
            time.sleep(interval)

    threading.Thread(target=_loop, name="fleet-reporter", daemon=True).start()
    _log.info("Fleet reporter started (every %ss to %s).", interval, settings.fleet_url)
    return True

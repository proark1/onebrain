"""The reporter: a deployment posts its own metadata-only heartbeat to Mission
Control on a timer.

`collect_heartbeat` builds the fleet.v2 body from this deployment's own GROUND
TRUTH — the CI-stamped build version (ONEBRAIN_BUILD_VERSION, falling back to
`app.__version__`), the alembic revision actually stamped in the live database
(pgvector mode; memory mode claims nothing), real store counts, env-gated
co-located module probes, the on-box update_state.json outcome channel, and
process uptime — pure of network, so it is unit-testable. `healthy` is
COMPUTED: any failing collector or a revision mismatch degrades it instead of
fabricating "true". Each collector is individually failure-isolated (`_safe`),
so one broken store degrades one field, never the beat. Unhealthy modules do
NOT zero the onebrain counts — module health is reported per-module and the
heartbeat's `healthy` property rolls it up. `auth_failures_recent` /
`api_5xx_recent` remain process-lifetime cumulative counters (unchanged v1
meaning); `uptime_seconds` is what makes a counter reset readable as a restart.

`send_heartbeat` does the one POST (stdlib urllib, no new dependency; the
opener is injectable for tests). `report_once` glues them and NEVER raises — a
reporting failure must not disturb the serving deployment. `start_reporter`
runs it on a daemon timer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone

from app import __version__ as app_version
from app.config import Settings
from app.db.schema import REQUIRED_ALEMBIC_REVISION, read_live_alembic_revision
from app.fleet.heartbeat import FleetHeartbeat, FleetHeartbeatV2, UpdateReport, build_heartbeat_v2
from app.fleet.module_probe import collect_module_reports
from app.fleet.update_state import read_update_report, update_state_path

_log = logging.getLogger("onebrain.fleet")

_PROCESS_START = time.monotonic()


def _safe(fn, default):
    """(value, ok) — the per-collector isolation primitive: one failing store
    call degrades that field (and flips healthy) instead of killing the beat."""
    try:
        return fn(), True
    except Exception:
        return default, False


def collect_heartbeat(settings: Settings, *, probe_opener=None) -> FleetHeartbeatV2:
    from app.deps import (
        get_intake_store, get_job_store, get_platform_store, get_service_key_store,
        get_store, get_user_store,
    )
    from app.monitoring import monitoring_snapshot

    chunks, ok_chunks = _safe(lambda: get_store().count(), 0)
    intake, ok_intake = _safe(lambda: get_intake_store().count(), 0)
    users, ok_users = _safe(lambda: get_user_store().count(), 0)
    accounts, ok_accounts = _safe(lambda: len(get_platform_store().list_accounts()), 0)
    keys, ok_keys = _safe(lambda: get_service_key_store().summary().active, 0)
    jobs, ok_jobs = _safe(lambda: get_job_store().summary(recent_failures_limit=0), None)
    metrics, _ = _safe(monitoring_snapshot, None)

    if settings.vector_store == "pgvector":
        revision, ok_rev_read = _safe(lambda: read_live_alembic_revision(settings.pg_database_url), "")
        revision_ok = ok_rev_read and revision == REQUIRED_ALEMBIC_REVISION
    else:
        revision, revision_ok = "", True     # memory mode: no schema to attest — claim nothing

    modules, _ = _safe(lambda: collect_module_reports(settings, opener=probe_opener), [])
    update, _ = _safe(lambda: read_update_report(update_state_path(settings.data_dir)), UpdateReport())

    healthy = all([ok_chunks, ok_intake, ok_users, ok_accounts, ok_keys, ok_jobs, revision_ok])
    return build_heartbeat_v2(
        deployment_id=settings.deployment_id,
        reported_at=datetime.now(timezone.utc).isoformat(),
        version=settings.build_version or app_version,
        migration_revision=revision,
        onebrain_healthy=healthy,
        chunks=chunks,
        intake_records=intake,
        users=users,
        accounts=accounts,
        active_service_keys=keys,
        jobs_pending=int(jobs.by_status.get("pending", 0)) if jobs else 0,
        jobs_failed=int(jobs.by_status.get("failed", 0)) if jobs else 0,
        auth_failures_recent=int(metrics.auth_total) if metrics else 0,
        api_5xx_recent=int(metrics.api_errors_5xx) if metrics else 0,
        uptime_seconds=int(time.monotonic() - _PROCESS_START),
        modules=modules,
        update=update,
    )


def send_heartbeat(fleet_url: str, fleet_key: str, heartbeat: FleetHeartbeat | FleetHeartbeatV2,
                   *, opener=None, timeout: float = 10.0) -> int:
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

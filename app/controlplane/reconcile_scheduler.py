"""Reconcile scheduler — Mission Control drives the pull-path reconcile tick on a
timer so a pull rollout converges without an operator hand-poking the manual
`POST /api/operator/fleet-rollouts/reconcile` endpoint.

`reconcile_once` is one tick: a NEVER-RAISING wrapper over `reconcile_pull_targets`
(P4-06) with the live clock + the latest heartbeats. `start_reconcile_scheduler`
runs it on a daemon thread, mirroring `app/fleet/retention.py:start_fleet_retention`
and `app/fleet/reporter.py:start_reporter` exactly (config interval, daemon thread,
never fatal, Mission-Control-only).

OFF BY DEFAULT (G3-4). `fleet_reconcile_seconds` defaults to 0 = disabled. The daemon
starts ONLY on an operator_mode instance that has EXPLICITLY set a positive interval,
so landing this package does NOT flip auto-advance on the already-deployed dormant MC
— auto-advance is opt-in, never turned on by a deploy.

Concurrency (G2-3). This daemon and a concurrent manual reconcile endpoint both drive
the read-modify-write reduce sequence with no cross-operation lock. That is benign
under the documented single-MC scope (terminal children are skipped and
`advance_fleet_rollout` is monotonic), so no lock is required in Phase 5 — but a
future multi-writer change (a second MC replica / leader election) MUST NOT break that
invariant. The scheduler drives the SAME `reconcile_pull_targets` ->
`reconcile_fleet_rollout` -> unchanged `advance_fleet_rollout` path the manual endpoint
uses; there is no parallel reducer.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.controlplane.pull_reconcile import reconcile_pull_targets

_log = logging.getLogger("onebrain.fleet")


def reconcile_once(settings, control_store, fleet_store) -> list:
    """One tick. A pure-ish, NEVER-RAISING wrapper over `reconcile_pull_targets` with
    the live clock + the latest heartbeats. Returns the reconciled fleet runs (``[]`` on
    any failure — a store that throws degrades to a no-op tick, never a crashed daemon).

    `dispatch_child` is `app.routers.operator.fleet_dispatch_child` (G2-3: it lives in
    `operator.py`, NOT `app/controlplane/fleet_runner`). It is imported LAZILY here so
    importing this module at startup never pulls the router graph in — mirroring the
    retention daemon's lazy `get_fleet_store` import and avoiding a router-import-at-
    startup cycle. Both the endpoint and this scheduler call the SAME dispatcher."""
    from app.routers.operator import fleet_dispatch_child

    try:
        return reconcile_pull_targets(
            control_store, control_store, fleet_store.latest_heartbeats(),
            now=datetime.now(timezone.utc),
            deadline_seconds=settings.fleet_pull_convergence_deadline_seconds,
            dispatch_child=fleet_dispatch_child)
    except Exception as exc:  # a reconcile failure must never kill the daemon
        _log.warning("Pull reconcile tick failed: %s", exc)
        return []


def start_reconcile_scheduler(settings) -> bool:
    """Daemon: drive the pull-path reconcile tick every `fleet_reconcile_seconds`.

    Mission Control only, and OPT-IN (G3-4): returns False (no thread) unless
    `operator_mode` is set AND `fleet_reconcile_seconds` is explicitly > 0. A positive
    interval is clamped to a 30s floor so a fat-fingered tiny value cannot hot-loop the
    reducer. Never fatal — each tick's failure is isolated inside `reconcile_once`."""
    if not settings.operator_mode or int(settings.fleet_reconcile_seconds) <= 0:
        return False
    interval = max(30, int(settings.fleet_reconcile_seconds))

    def _loop() -> None:
        from app.deps import get_control_plane_store, get_fleet_store

        while True:
            try:
                reconcile_once(settings, get_control_plane_store(), get_fleet_store())
            except Exception as exc:  # pragma: no cover - defensive (store getters)
                _log.warning("Pull reconcile tick failed: %s", exc)
            time.sleep(interval)

    threading.Thread(target=_loop, name="fleet-reconcile", daemon=True).start()
    _log.info("Fleet reconcile scheduler started (every %ss).", interval)
    return True

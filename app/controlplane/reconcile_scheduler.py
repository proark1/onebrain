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
from contextlib import contextmanager
from datetime import datetime, timezone

from app.controlplane.pull_reconcile import reconcile_pull_targets

_log = logging.getLogger("onebrain.fleet")
_LOCAL_RECONCILE_LOCK = threading.Lock()
# Stable, namespaced 64-bit key for the one logical Mission Control reconcile
# leader. A held session lock is automatically released if its replica dies.
_RECONCILE_ADVISORY_LOCK_KEY = 4_822_021_191


@contextmanager
def _reconcile_leadership(settings):
    """Yield whether this process may run a reconcile reducer tick.

    Production pgvector deployments share the PostgreSQL advisory lock across
    API replicas. Local/test stores use a process-local lock so a scheduler and
    a manual request in the same process still cannot interleave reducers.
    Failure to establish production leadership fails closed to a no-op.
    """
    if getattr(settings, "vector_store", "memory") != "pgvector":
        acquired = _LOCAL_RECONCILE_LOCK.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                _LOCAL_RECONCILE_LOCK.release()
        return

    dsn = (
        getattr(settings, "pg_operator_database_url", "")
        or getattr(settings, "operator_database_url", "")
        or getattr(settings, "database_url", "")
    )
    if not dsn:
        _log.warning("Pull reconcile skipped: no PostgreSQL leadership DSN configured")
        yield False
        return
    try:
        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_RECONCILE_ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
    except Exception as exc:
        _log.warning("Pull reconcile skipped: unable to acquire leadership: %s", exc)
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass
        yield False
        return

    acquired = bool(row and row[0])
    try:
        yield acquired
    finally:
        try:
            if acquired:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_RECONCILE_ADVISORY_LOCK_KEY,))
        finally:
            try:
                cur.close()
            finally:
                conn.close()


def _operator_self_deployment_id(settings) -> str:
    """MC's own deployment id when operator self-deploy is enabled, else "" — the pull
    reconcile then also converges MC's own self-update rollout. Dormant (returns "")
    unless BOTH operator_mode and operator_auto_deploy_enabled are set, so a customer
    box or an MC with the feature off drives the tick exactly as before."""
    if not (getattr(settings, "operator_mode", False)
            and getattr(settings, "operator_auto_deploy_enabled", False)):
        return ""
    return (getattr(settings, "deployment_id", "") or "").strip()


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
        with _reconcile_leadership(settings) as leader:
            if not leader:
                return []
            runs = reconcile_pull_targets(
                control_store, control_store, fleet_store.latest_heartbeats(),
                now=datetime.now(timezone.utc),
                deadline_seconds=settings.fleet_pull_convergence_deadline_seconds,
                dispatch_child=fleet_dispatch_child,
                operator_self_deployment_id=_operator_self_deployment_id(settings))
            # Self-healing development pipeline (roadmap Gap A). Same leader, same tick:
            # reclaim any development candidate stranded at dev_failed for a TRANSIENT
            # reason. Isolated below so a retry failure never discards `runs`.
            _run_development_auto_retry(settings, control_store)
            return runs
    except Exception as exc:  # a reconcile failure must never kill the daemon
        _log.warning("Pull reconcile tick failed: %s", exc)
        return []


def _run_development_auto_retry(settings, control_store) -> None:
    """Reclaim transiently-failed development candidates (roadmap Gap A).

    Opt-in (``development_auto_retry_enabled``) and never-raising: a store or dispatch
    failure degrades to a no-op tick, never a crashed daemon. The dispatcher is imported
    LAZILY so importing this module at startup never pulls the router graph in — the same
    reason reconcile_once imports fleet_dispatch_child lazily. Both re-dispatch through the
    SAME ``_dispatch_development_candidate`` path the operator's retry-dev endpoint uses."""
    if not getattr(settings, "development_auto_retry_enabled", False):
        return
    try:
        from app.controlplane.development_retry import reclaim_retryable_development_candidates
        from app.routers.operator import _dispatch_development_candidate

        reclaim_retryable_development_candidates(
            control_store,
            now=datetime.now(timezone.utc),
            max_attempts=int(getattr(settings, "development_auto_retry_max_attempts", 5)),
            backoff_seconds=int(getattr(settings, "development_auto_retry_backoff_seconds", 600)),
            backup_backoff_seconds=int(
                getattr(settings, "development_auto_retry_backup_backoff_seconds", 21600)
            ),
            dispatch=_dispatch_development_candidate,
        )
    except Exception as exc:  # never let auto-retry break the reconcile tick
        _log.warning("Development auto-retry tick failed: %s", exc)


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

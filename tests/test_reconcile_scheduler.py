"""P5-04: the in-process pull-reconcile scheduler. `reconcile_once` is exercised
directly (a store-throwing case proves it never raises); `start_reconcile_scheduler`
is asserted only for its OPT-IN gate (G3-4) — no real thread is started in tests,
exactly as `start_reporter` / `start_fleet_retention` are tested.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from app.controlplane import reconcile_scheduler
from tests.test_pull_reconcile import DEADLINE, _hb, _store_with_offered_child


def _settings(**kw):
    base = dict(operator_mode=True, fleet_reconcile_seconds=60,
               fleet_pull_convergence_deadline_seconds=DEADLINE)
    base.update(kw)
    return SimpleNamespace(**base)


# --- reconcile_once (one tick, driven directly — no thread) -------------------

def test_reconcile_once_advances_fleet_run_on_success():
    store = _store_with_offered_child()
    fleet = SimpleNamespace(
        latest_heartbeats=lambda: {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded")})

    runs = reconcile_scheduler.reconcile_once(_settings(), store, fleet)

    # Drives the SAME reducer the manual endpoint uses: the offered pull child goes
    # terminal and the single-ring fleet run advances to succeeded.
    assert [r.id for r in runs] == ["f1"]
    assert store.get_rollout("c_dep").status == "success"
    assert store.get_fleet_rollout("f1").status == "succeeded"


def test_reconcile_once_at_rest_is_noop():
    from app.controlplane.memory import MemoryControlPlaneStore

    store = MemoryControlPlaneStore()  # no fleet rollouts
    fleet = SimpleNamespace(latest_heartbeats=lambda: {})
    assert reconcile_scheduler.reconcile_once(_settings(), store, fleet) == []


def test_reconcile_once_never_raises_when_store_throws():
    """A store whose latest_heartbeats() raises degrades to a no-op tick ([]),
    never an exception — the daemon must survive a transient store failure."""
    class _Boom:
        def latest_heartbeats(self):
            raise RuntimeError("store unavailable")

    store = _store_with_offered_child()
    runs = reconcile_scheduler.reconcile_once(_settings(), store, _Boom())

    assert runs == []
    # The store failed before any child was touched — nothing was driven terminal.
    assert store.get_rollout("c_dep").status == "pending"


def test_reconcile_once_skips_when_another_postgres_replica_is_leader(monkeypatch):
    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))

        def fetchone(self):
            return (False,)

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()
            self.closed = False

        def cursor(self):
            return self.cursor_value

        def close(self):
            self.closed = True

    class Psycopg:
        def __init__(self):
            self.connection = Connection()
            self.calls = []

        def connect(self, dsn, autocommit=False):
            self.calls.append((dsn, autocommit))
            return self.connection

    psycopg = Psycopg()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    store = _store_with_offered_child()
    fleet = SimpleNamespace(latest_heartbeats=lambda: {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded")})
    settings = _settings(vector_store="pgvector", pg_operator_database_url="postgresql://leader-test")

    assert reconcile_scheduler.reconcile_once(settings, store, fleet) == []
    assert psycopg.calls == [("postgresql://leader-test", True)]
    assert "pg_try_advisory_lock" in psycopg.connection.cursor_value.calls[0][0]
    assert store.get_rollout("c_dep").status == "pending"


# --- development auto-retry wiring (roadmap Gap A) ----------------------------

_ORCHESTRATOR = "app.controlplane.development_retry.reclaim_retryable_development_candidates"


def test_auto_retry_is_a_noop_when_the_flag_is_off(monkeypatch):
    called = []
    monkeypatch.setattr(_ORCHESTRATOR, lambda *a, **k: called.append(True) or [])
    # _settings() has no development_auto_retry_enabled attribute → getattr default False.
    reconcile_scheduler._run_development_auto_retry(_settings(), object())
    assert called == []


def test_auto_retry_invokes_the_orchestrator_with_configured_bounds(monkeypatch):
    captured = {}

    def _fake(store, **kwargs):
        captured["store"] = store
        captured.update(kwargs)
        return []

    monkeypatch.setattr(_ORCHESTRATOR, _fake)
    store = object()
    reconcile_scheduler._run_development_auto_retry(
        _settings(development_auto_retry_enabled=True,
                  development_auto_retry_max_attempts=3,
                  development_auto_retry_backoff_seconds=120,
                  development_auto_retry_backup_backoff_seconds=3600),
        store)
    assert captured["store"] is store
    assert captured["max_attempts"] == 3
    assert captured["backoff_seconds"] == 120
    assert captured["backup_backoff_seconds"] == 3600
    assert callable(captured["dispatch"])  # the real _dispatch_development_candidate


def test_auto_retry_never_raises_out_of_the_tick(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_ORCHESTRATOR, _boom)
    # Must swallow — a retry failure can never kill the reconcile daemon.
    reconcile_scheduler._run_development_auto_retry(
        _settings(development_auto_retry_enabled=True), object())


def test_reconcile_once_runs_auto_retry_and_still_returns_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(_ORCHESTRATOR, lambda store, **k: calls.append(store) or [])
    store = _store_with_offered_child()
    fleet = SimpleNamespace(
        latest_heartbeats=lambda: {"dep_p": _hb("dep_p", attempt_id="c_dep", outcome="succeeded")})

    runs = reconcile_scheduler.reconcile_once(
        _settings(development_auto_retry_enabled=True), store, fleet)

    # The reconcile result is preserved (auto-retry runs AFTER it, isolated) and the
    # orchestrator ran on the same store within the same leader-held tick.
    assert [r.id for r in runs] == ["f1"]
    assert calls == [store]


# --- start_reconcile_scheduler (gate only — no real thread) -------------------

def test_start_reconcile_scheduler_returns_false_off_operator():
    assert reconcile_scheduler.start_reconcile_scheduler(
        _settings(operator_mode=False, fleet_reconcile_seconds=60)) is False


def test_start_reconcile_scheduler_returns_false_when_interval_zero():
    # G3-4: operator_mode alone does NOT start the daemon — the interval must be > 0.
    assert reconcile_scheduler.start_reconcile_scheduler(
        _settings(operator_mode=True, fleet_reconcile_seconds=0)) is False

"""P5-04: the in-process pull-reconcile scheduler. `reconcile_once` is exercised
directly (a store-throwing case proves it never raises); `start_reconcile_scheduler`
is asserted only for its OPT-IN gate (G3-4) — no real thread is started in tests,
exactly as `start_reporter` / `start_fleet_retention` are tested.
"""

from __future__ import annotations

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


# --- start_reconcile_scheduler (gate only — no real thread) -------------------

def test_start_reconcile_scheduler_returns_false_off_operator():
    assert reconcile_scheduler.start_reconcile_scheduler(
        _settings(operator_mode=False, fleet_reconcile_seconds=60)) is False


def test_start_reconcile_scheduler_returns_false_when_interval_zero():
    # G3-4: operator_mode alone does NOT start the daemon — the interval must be > 0.
    assert reconcile_scheduler.start_reconcile_scheduler(
        _settings(operator_mode=True, fleet_reconcile_seconds=0)) is False

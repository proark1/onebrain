"""Release-pipeline stall alerts (roadmap Gap D — detection).

Pins the two pipeline signals — a development candidate stuck at dev_failed, and Mission
Control's own self-deploy giving up — plus the invariant that this watchdog and the
heartbeat watchdog share Mission Control's alert row without clobbering each other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.controlplane.base import (
    CustomerDeployment,
    ReleaseManifest,
    ReleasePromotion,
    ReleasePromotionEvent,
    RolloutRun,
)
from app.controlplane.memory import MemoryControlPlaneStore
from app.fleet.base import (
    DEV_PIPELINE_STALLED_ALERT,
    GATE_REPLACEMENT_RECOMMENDED_ALERT,
    OPERATOR_SELF_DEPLOY_STALLED_ALERT,
    FleetAlert,
    Heartbeat,
)
from app.fleet.heartbeat import CONTRACT_VERSION_V2
from app.fleet.memory import MemoryFleetStore
from app.fleet.pipeline_watchdog import desired_pipeline_alerts, run_pipeline_watchdog
from app.fleet.watchdog import run_watchdog

MC = "mc"
NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(**kwargs) -> str:
    return _iso(NOW - timedelta(**kwargs))


def _control(*, mc_version="2026.07.22.400") -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id=MC, customer_name=MC, deployment_type="dedicated_server", current_version=mc_version,
    ))
    return store


def _candidate(store, version, *, state, failure_reason="", failed_at="", note=""):
    ts = "2026-07-20T00:00:00+00:00"
    store.create_release_candidate(
        ReleaseManifest(version=version, git_sha="a" * 40, modules={"onebrain-api": version}),
        ReleasePromotion(release_version=version, state=state, failure_reason=failure_reason,
                         dev_completed_at=failed_at, created_at=ts, updated_at=failed_at or ts),
        ReleasePromotionEvent(id="", release_version=version, action="seed", to_state=state,
                              note=note, created_at=failed_at or ts),
    )


def _desired(store, *, stall_seconds=10800, self_deploy_enabled=False, self_max_attempts=3):
    return desired_pipeline_alerts(
        store, now=NOW, mc_deployment_id=MC, stall_seconds=stall_seconds,
        self_deploy_enabled=self_deploy_enabled, self_max_attempts=self_max_attempts,
    )


# --- dev-pipeline-stalled signal ---------------------------------------------

def test_stall_alert_opens_for_a_stuck_dev_failed_candidate():
    store = _control()
    _candidate(store, "2026.07.22.500", state="dev_failed",
               failure_reason="dev_rollout_failed", failed_at=_ago(hours=4))
    alerts = _desired(store, stall_seconds=10800)  # 3h threshold, candidate 4h old
    assert DEV_PIPELINE_STALLED_ALERT in alerts
    assert "2026.07.22.500" in alerts[DEV_PIPELINE_STALLED_ALERT]


def test_stall_alert_respects_the_threshold():
    store = _control()
    _candidate(store, "2026.07.22.500", state="dev_failed",
               failure_reason="dev_rollout_failed", failed_at=_ago(hours=1))
    assert DEV_PIPELINE_STALLED_ALERT not in _desired(store, stall_seconds=10800)  # only 1h old


def test_stall_alert_disabled_when_threshold_is_zero():
    store = _control()
    _candidate(store, "2026.07.22.500", state="dev_failed",
               failure_reason="dev_rollout_failed", failed_at=_ago(hours=9))
    assert DEV_PIPELINE_STALLED_ALERT not in _desired(store, stall_seconds=0)


def test_stall_alert_excludes_a_superseded_candidate():
    store = _control()
    _candidate(store, "2026.07.22.486", state="dev_failed",
               failure_reason="dev_rollout_failed", failed_at=_ago(hours=9))
    _candidate(store, "2026.07.22.500", state="dev_verified")   # newer, verified
    assert DEV_PIPELINE_STALLED_ALERT not in _desired(store)


def test_stall_alert_excludes_a_backup_waiter():
    store = _control()
    # Recorded generically as dev_preflight_failed with the real reason in the event note.
    _candidate(store, "2026.07.22.501", state="dev_failed",
               failure_reason="dev_preflight_failed", failed_at=_ago(hours=9),
               note="backup_required_for_schema_update")
    assert DEV_PIPELINE_STALLED_ALERT not in _desired(store)


def test_stall_alert_names_the_oldest_and_counts_all_stalled():
    store = _control()
    _candidate(store, "2026.07.22.500", state="dev_failed",
               failure_reason="dev_rollout_failed", failed_at=_ago(hours=4))
    _candidate(store, "2026.07.22.501", state="dev_failed",
               failure_reason="dev_dispatch_failed", failed_at=_ago(hours=9))
    detail = _desired(store)[DEV_PIPELINE_STALLED_ALERT]
    assert "2026.07.22.501" in detail       # the oldest-stalled leads
    assert "2 candidates stalled" in detail


# --- operator-self-deploy-stalled signal -------------------------------------

def _seed_failed_self_rollouts(store, version, count):
    for index in range(count):
        store._rollouts[f"roll_mc_{index}"] = RolloutRun(
            id=f"roll_mc_{index}", deployment_id=MC, target_version=version,
            status="failed", started_by="operator-self:test",
        )


def test_self_deploy_stalled_alert_when_budget_exhausted():
    store = _control(mc_version="2026.07.22.400")
    _candidate(store, "2026.07.22.500", state="dev_verified")
    _seed_failed_self_rollouts(store, "2026.07.22.500", 3)
    alerts = _desired(store, self_deploy_enabled=True, self_max_attempts=3)
    assert OPERATOR_SELF_DEPLOY_STALLED_ALERT in alerts
    assert "2026.07.22.500" in alerts[OPERATOR_SELF_DEPLOY_STALLED_ALERT]


def test_self_deploy_stalled_requires_auto_deploy_enabled():
    store = _control(mc_version="2026.07.22.400")
    _candidate(store, "2026.07.22.500", state="dev_verified")
    _seed_failed_self_rollouts(store, "2026.07.22.500", 3)
    # Auto-deploy OFF → MC lagging the tip is expected, not a stall.
    assert OPERATOR_SELF_DEPLOY_STALLED_ALERT not in _desired(store, self_deploy_enabled=False)


def test_self_deploy_not_stalled_below_budget_or_when_on_target():
    store = _control(mc_version="2026.07.22.400")
    _candidate(store, "2026.07.22.500", state="dev_verified")
    _seed_failed_self_rollouts(store, "2026.07.22.500", 2)   # only 2 of 3
    assert OPERATOR_SELF_DEPLOY_STALLED_ALERT not in _desired(
        store, self_deploy_enabled=True, self_max_attempts=3)
    # Once MC is ON the target, exhausted history is moot.
    on_target = _control(mc_version="2026.07.22.500")
    _candidate(on_target, "2026.07.22.500", state="dev_verified")
    _seed_failed_self_rollouts(on_target, "2026.07.22.500", 5)
    assert OPERATOR_SELF_DEPLOY_STALLED_ALERT not in _desired(
        on_target, self_deploy_enabled=True, self_max_attempts=3)


# --- gate-replacement-recommended signal (Phase 4 Tier 1) --------------------

def _gate(store, gate_id="gate"):
    """Create and designate an active development gate; return its id."""
    store.create_deployment(CustomerDeployment(
        id=gate_id, customer_name=gate_id, deployment_type="dedicated_server",
        environment="development",
    ))
    store.designate_release_gate(gate_id)
    return gate_id


def _gate_alert(fleet, gate_id, kind, created_at):
    fleet.open_alert(FleetAlert(id=f"fa_{gate_id}_{kind}", deployment_id=gate_id, kind=kind,
                                detail=kind, status="open", created_at=created_at))


def _desired_gate(store, fleet, *, gate_replace_seconds=1800):
    return desired_pipeline_alerts(
        store, now=NOW, mc_deployment_id=MC, stall_seconds=0,
        self_deploy_enabled=False, self_max_attempts=3,
        fleet_store=fleet, gate_replace_seconds=gate_replace_seconds,
    )


def test_gate_replacement_alert_opens_on_sustained_gate_failure():
    store = _control()
    gate = _gate(store)
    fleet = MemoryFleetStore()
    _gate_alert(fleet, gate, "missed_heartbeat", _ago(hours=1))   # older than the 30m threshold
    alerts = _desired_gate(store, fleet)
    assert GATE_REPLACEMENT_RECOMMENDED_ALERT in alerts
    detail = alerts[GATE_REPLACEMENT_RECOMMENDED_ALERT]
    assert gate in detail and "missed_heartbeat" in detail


def test_gate_replacement_alert_fires_on_data_volume_unavailable():
    store = _control()
    gate = _gate(store)
    fleet = MemoryFleetStore()
    _gate_alert(fleet, gate, "data_volume_unavailable", _ago(hours=1))   # disk death, 2026-07-22 case
    assert GATE_REPLACEMENT_RECOMMENDED_ALERT in _desired_gate(store, fleet)


def test_gate_replacement_alert_respects_threshold():
    store = _control()
    gate = _gate(store)
    fleet = MemoryFleetStore()
    _gate_alert(fleet, gate, "missed_heartbeat", _ago(minutes=10))   # younger than 30m — a blip
    assert GATE_REPLACEMENT_RECOMMENDED_ALERT not in _desired_gate(store, fleet)


def test_gate_replacement_alert_ignores_soft_unhealthy():
    store = _control()
    gate = _gate(store)
    fleet = MemoryFleetStore()
    # `unhealthy` alone is soft (an app restart self-recovers) — must NOT recommend a replace.
    _gate_alert(fleet, gate, "unhealthy", _ago(hours=4))
    assert GATE_REPLACEMENT_RECOMMENDED_ALERT not in _desired_gate(store, fleet)


def test_gate_replacement_alert_disabled_when_seconds_zero():
    store = _control()
    gate = _gate(store)
    fleet = MemoryFleetStore()
    _gate_alert(fleet, gate, "missed_heartbeat", _ago(hours=9))
    assert GATE_REPLACEMENT_RECOMMENDED_ALERT not in _desired_gate(store, fleet, gate_replace_seconds=0)


def test_gate_replacement_alert_none_without_a_designated_gate():
    store = _control()   # no gate designated
    fleet = MemoryFleetStore()
    # A stray failure on a non-gate box must not trigger the recommendation.
    _gate_alert(fleet, "some-box", "missed_heartbeat", _ago(hours=9))
    assert GATE_REPLACEMENT_RECOMMENDED_ALERT not in _desired_gate(store, fleet)


# --- reconcile (open / resolve) ----------------------------------------------

def _run(control, fleet, **kwargs):
    return run_pipeline_watchdog(
        control, fleet, now_iso=_iso(NOW), mc_deployment_id=MC,
        stall_seconds=kwargs.get("stall_seconds", 10800),
        self_deploy_enabled=kwargs.get("self_deploy_enabled", False),
        self_max_attempts=kwargs.get("self_max_attempts", 3),
        gate_replace_seconds=kwargs.get("gate_replace_seconds", 0),
        next_id=lambda: f"fa_{len(fleet.list_open_alerts()) + 1}",
    )


def test_gate_replacement_reconcile_opens_on_mc_row_then_resolves():
    control = _control()
    gate = _gate(control)
    fleet = MemoryFleetStore()
    _gate_alert(fleet, gate, "missed_heartbeat", _ago(hours=1))
    opened = _run(control, fleet, gate_replace_seconds=1800)
    # The recommendation opens on MC's OWN row (surfaces in the overview), naming the gate.
    assert [a.kind for a in opened] == [GATE_REPLACEMENT_RECOMMENDED_ALERT]
    assert fleet.has_open_alert(MC, GATE_REPLACEMENT_RECOMMENDED_ALERT)
    # The gate's own infra alert on the gate's row is left untouched.
    assert fleet.has_open_alert(gate, "missed_heartbeat")
    # Idempotent second tick.
    assert _run(control, fleet, gate_replace_seconds=1800) == []
    # The gate recovers (its infra alert clears) → the recommendation resolves.
    fleet.resolve_open_alerts(gate, "missed_heartbeat", _iso(NOW))
    _run(control, fleet, gate_replace_seconds=1800)
    assert not fleet.has_open_alert(MC, GATE_REPLACEMENT_RECOMMENDED_ALERT)


def test_reconcile_opens_then_resolves_a_stall_alert():
    control = _control()
    _candidate(control, "2026.07.22.500", state="dev_failed",
               failure_reason="dev_rollout_failed", failed_at=_ago(hours=4))
    fleet = MemoryFleetStore()
    opened = _run(control, fleet)
    assert [a.kind for a in opened] == [DEV_PIPELINE_STALLED_ALERT]
    assert fleet.has_open_alert(MC, DEV_PIPELINE_STALLED_ALERT)
    # Idempotent: a second tick opens nothing new.
    assert _run(control, fleet) == []
    # The candidate verifies → the stall alert resolves.
    control.transition_release_promotion(
        "2026.07.22.500", frozenset({"dev_failed"}), "dev_pending",
        actor="op", action="retry")
    control.transition_release_promotion(
        "2026.07.22.500", frozenset({"dev_pending"}), "dev_deploying",
        actor="op", action="dispatch")
    control.transition_release_promotion(
        "2026.07.22.500", frozenset({"dev_deploying"}), "dev_verified",
        actor="op", action="verify")
    _run(control, fleet)
    assert not fleet.has_open_alert(MC, DEV_PIPELINE_STALLED_ALERT)


def test_pipeline_watchdog_leaves_infra_alerts_untouched():
    control = _control()
    fleet = MemoryFleetStore()
    # An infra alert already open on MC's row (opened by the heartbeat watchdog).
    fleet.open_alert(FleetAlert(id="fa_infra", deployment_id=MC, kind="missed_heartbeat",
                                detail="silent", status="open", created_at=_iso(NOW)))
    _run(control, fleet)   # no pipeline signals wanted, and it must not resolve the infra one
    assert fleet.has_open_alert(MC, "missed_heartbeat")


# --- coexistence with the heartbeat watchdog ---------------------------------

def test_run_watchdog_does_not_resolve_a_pipeline_alert():
    # The reciprocal guard: run_watchdog manages only its own infra kinds, so a pipeline
    # alert on the same deployment row survives a heartbeat-watchdog pass.
    fleet = MemoryFleetStore()
    fleet.open_alert(FleetAlert(id="fa_pipe", deployment_id=MC, kind=DEV_PIPELINE_STALLED_ALERT,
                                detail="stuck", status="open", created_at=_iso(NOW)))
    # A healthy, current heartbeat means NO infra alert is wanted — so run_watchdog's resolve
    # loop would clear every open alert on the row unless it scopes to its own kinds. The
    # foreign pipeline kind must survive.
    fleet.record_heartbeat(Heartbeat("hb", MC, CONTRACT_VERSION_V2, _iso(NOW), _iso(NOW), True, payload={}))
    counter = {"n": 0}

    def _next_id():
        counter["n"] += 1
        return f"fa_{counter['n']}"

    run_watchdog(fleet, [MC], now_iso=_iso(NOW), missed_after_seconds=600, next_id=_next_id)
    assert fleet.has_open_alert(MC, DEV_PIPELINE_STALLED_ALERT)

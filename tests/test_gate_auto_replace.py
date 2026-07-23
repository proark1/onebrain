"""Gate auto-replacement daemon (roadmap Phase 4, Gap E2, Tier 2).

Two layers, mirroring the module: the PURE policy (``decide_gate_replacement``) exercised as a
truth table, and the orchestrator (``run_gate_auto_replace_tick``) driven against real in-memory
stores with stubbed provision/designate side effects — so the world-derived sequence
(provision -> wait -> designate -> recommend-reap) and the cost-runaway rails are pinned without
ever touching a broker. The daemon NEVER tears anything down; that stays a manual action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.controlplane.base import CustomerDeployment
from app.controlplane.gate_auto_replace import (
    GATE_AUTO_REPLACE_ACTOR,
    decide_gate_replacement,
    gate_auto_replace_once,
    run_gate_auto_replace_tick,
)
from app.fleet.base import (
    DEV_PIPELINE_STALLED_ALERT,
    GATE_AUTO_REPLACE_WEDGED_ALERT,
    GATE_DECOMMISSION_RECOMMENDED_ALERT,
    FleetAlert,
)
from app.fleet.memory import MemoryFleetStore
from app.controlplane.memory import MemoryControlPlaneStore
from app.provisioning.runs import MemoryProvisioningRunStore, ProvisioningRun
from app.routers.operator import DEVELOPMENT_GATE_DEPLOYMENT_ID as BASE
from app.routers.operator import _is_live_gate_replacement

MC = "mc"
NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(**kwargs) -> str:
    return _iso(NOW - timedelta(**kwargs))


# --- pure policy: decide_gate_replacement ------------------------------------

def _decide(**over):
    """Default world = gate sustained-dead, no replacement in flight, every guard clear ->
    the baseline decision is PROVISION. Each test flips exactly one axis."""
    kwargs = dict(
        now=NOW, mc_deployment_id=MC,
        gate=SimpleNamespace(id="gate", region="fsn1"),
        gate_sustained_failure=("missed_heartbeat", 3600.0),
        live_replacement=None, live_replacement_created_at=None, live_replacement_blockers=[],
        reap_details={}, last_attempt_at=None, live_deployment_count=2, max_fleet_servers=5,
        min_interval_seconds=21600, replace_timeout_seconds=3600, provisioner_ready=True,
        owner_email_available=True, baseline_ready=True, replacement_region="fsn1",
    )
    kwargs.update(over)
    return decide_gate_replacement(**kwargs)


def test_provision_when_dead_and_every_guard_clear():
    decision = _decide()
    assert decision.action == "provision"
    assert decision.provision_region == "fsn1"
    assert decision.wedge_detail is None


def test_noop_when_gate_healthy():
    decision = _decide(gate_sustained_failure=None)
    assert decision.action == "noop"
    assert decision.wedge_detail is None


def test_noop_when_no_gate_designated():
    # A missing gate is NOT bootstrapped from zero — Tier 2 only replaces a dying gate.
    decision = _decide(gate=None, gate_sustained_failure=None)
    assert decision.action == "noop"


def test_noop_when_provisioner_not_hetzner():
    decision = _decide(provisioner_ready=False)
    assert decision.action == "noop"
    assert decision.wedge_detail is None   # stay quiet; Tier 1 alert already surfaces the dead gate


def test_wedge_at_server_cap():
    decision = _decide(live_deployment_count=5, max_fleet_servers=5)
    assert decision.action == "noop"
    assert "server cap" in decision.wedge_detail


def test_cap_disabled_when_zero_still_provisions():
    decision = _decide(live_deployment_count=99, max_fleet_servers=0)
    assert decision.action == "provision"


def test_wedge_when_no_owner_email():
    decision = _decide(owner_email_available=False)
    assert decision.action == "noop"
    assert "owner email" in decision.wedge_detail


def test_wedge_when_no_trusted_baseline():
    decision = _decide(baseline_ready=False)
    assert decision.action == "noop"
    assert "baseline" in decision.wedge_detail


def test_noop_min_interval_debounce():
    decision = _decide(last_attempt_at=NOW - timedelta(hours=1))   # < 6h
    assert decision.action == "noop"
    assert "debounce" in decision.reason
    assert decision.wedge_detail is None


def test_provision_when_min_interval_elapsed():
    decision = _decide(last_attempt_at=NOW - timedelta(hours=7))   # > 6h
    assert decision.action == "provision"


def test_designate_when_replacement_ready():
    replacement = SimpleNamespace(id="gate-abc", region="fsn1")
    decision = _decide(live_replacement=replacement, live_replacement_blockers=[])
    assert decision.action == "designate"
    assert decision.replacement_id == "gate-abc"


def test_wait_when_replacement_not_yet_ready():
    replacement = SimpleNamespace(id="gate-abc")
    decision = _decide(
        live_replacement=replacement, live_replacement_blockers=["deployment_unhealthy"],
        live_replacement_created_at=NOW - timedelta(minutes=5), replace_timeout_seconds=3600)
    assert decision.action == "noop"
    assert decision.wedge_detail is None   # still booting — not wedged yet


def test_orphan_wedge_when_replacement_times_out():
    replacement = SimpleNamespace(id="gate-abc")
    decision = _decide(
        live_replacement=replacement, live_replacement_blockers=["deployment_heartbeat_stale"],
        live_replacement_created_at=NOW - timedelta(hours=2), replace_timeout_seconds=3600)
    assert decision.action == "noop"
    assert "STOPPED" in decision.wedge_detail
    assert "gate-abc" in decision.wedge_detail


def test_one_in_flight_never_provisions_even_when_guards_clear():
    # A live replacement present -> NEVER mint a second box, no matter how clear the guards.
    replacement = SimpleNamespace(id="gate-abc")
    decision = _decide(
        live_replacement=replacement, live_replacement_blockers=["deployment_unhealthy"],
        live_replacement_created_at=NOW - timedelta(minutes=1),
        last_attempt_at=None, live_deployment_count=1, baseline_ready=True)
    assert decision.action != "provision"


def test_reap_details_ride_every_decision():
    reap = {"old-gate": "safe to decommission"}
    assert _decide(reap_details=reap).reap_details == reap                       # on provision
    assert _decide(gate_sustained_failure=None, reap_details=reap).reap_details == reap  # on healthy noop


# --- orchestrator: run_gate_auto_replace_tick --------------------------------

def _stores():
    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id=MC, customer_name=MC, deployment_type="dedicated_server"))
    return control, MemoryFleetStore(), MemoryProvisioningRunStore()


def _designate_gate(control, gate_id=BASE, *, region="fsn1", created_at=""):
    control.create_deployment(CustomerDeployment(
        id=gate_id, customer_name=gate_id, deployment_type="dedicated_server",
        environment="development", region=region, created_at=created_at or _ago(days=30)))
    control.designate_release_gate(gate_id)
    return gate_id


def _hard_alert(fleet, deployment_id, kind="missed_heartbeat", created_at=None):
    fleet.open_alert(FleetAlert(
        id=f"fa_{deployment_id}_{kind}", deployment_id=deployment_id, kind=kind,
        detail=kind, status="open", created_at=created_at or _ago(hours=1)))


def _next_id():
    counter = {"i": 0}

    def _make() -> str:
        counter["i"] += 1
        return f"fa_{counter['i']}"

    return _make


def _tick(control, fleet, runs, *, blockers_for=None, baseline_ready=None,
          provision=None, designate=None, **over):
    calls = {"provision": [], "designate": []}
    kwargs = dict(
        now_iso=_iso(NOW), mc_deployment_id=MC, gate_base_id=BASE,
        sustained_after_seconds=over.get("sustained_after_seconds", 1800),
        min_interval_seconds=over.get("min_interval_seconds", 21600),
        replace_timeout_seconds=over.get("replace_timeout_seconds", 3600),
        max_fleet_servers=over.get("max_fleet_servers", 5),
        owner_email=over.get("owner_email", "admin@onebrain.test"),
        provisioner_ready=over.get("provisioner_ready", True),
        blockers_for=blockers_for or (lambda deployment: []),
        is_live_replacement=_is_live_gate_replacement,
        baseline_ready=baseline_ready or (lambda: True),
        provision=provision or (lambda email, region: calls["provision"].append((email, region))),
        designate=designate or (lambda deployment_id: calls["designate"].append(deployment_id)),
        next_id=_next_id(),
    )
    decision, opened = run_gate_auto_replace_tick(control, fleet, runs, **kwargs)
    return decision, opened, calls


def test_tick_provisions_on_sustained_dead_gate():
    control, fleet, runs = _stores()
    _designate_gate(control, region="hel1")
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))
    decision, _opened, calls = _tick(control, fleet, runs)
    assert decision.action == "provision"
    assert calls["provision"] == [("admin@onebrain.test", "hel1")]   # region inherited from the dead gate


def test_tick_does_not_provision_when_gate_healthy():
    control, fleet, runs = _stores()
    _designate_gate(control)   # no failure alert -> healthy
    decision, _opened, calls = _tick(control, fleet, runs)
    assert decision.action == "noop"
    assert calls["provision"] == []


def test_tick_detection_disabled_when_sustained_zero():
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=9))
    decision, _opened, calls = _tick(control, fleet, runs, sustained_after_seconds=0)
    assert decision.action == "noop"
    assert calls["provision"] == []


def test_tick_one_in_flight_blocks_second_provision():
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))
    # A live (active, no dead run) suffixed replacement already exists.
    control.create_deployment(CustomerDeployment(
        id=f"{BASE}-abc", customer_name="replacement", deployment_type="dedicated_server",
        environment="development", created_at=_ago(minutes=5)))
    decision, _opened, calls = _tick(
        control, fleet, runs, blockers_for=lambda d: ["deployment_unhealthy"])
    assert calls["provision"] == []          # one-in-flight
    assert decision.action == "noop"         # waiting for the replacement to go healthy


def test_tick_designates_ready_replacement():
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))
    control.create_deployment(CustomerDeployment(
        id=f"{BASE}-abc", customer_name="replacement", deployment_type="dedicated_server",
        environment="development", created_at=_ago(minutes=20)))
    decision, _opened, calls = _tick(control, fleet, runs, blockers_for=lambda d: [])
    assert decision.action == "designate"
    assert calls["designate"] == [f"{BASE}-abc"]


def test_tick_opens_and_resolves_wedged_alert_at_cap():
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))
    # MC + gate = 2 live rows; cap at 2 -> at capacity.
    _decision, opened, calls = _tick(control, fleet, runs, max_fleet_servers=2)
    assert calls["provision"] == []
    assert any(a.kind == GATE_AUTO_REPLACE_WEDGED_ALERT for a in opened)
    assert fleet.has_open_alert(MC, GATE_AUTO_REPLACE_WEDGED_ALERT)
    # Raise the cap -> the wedge resolves and provisioning proceeds.
    _decision2, _opened2, calls2 = _tick(control, fleet, runs, max_fleet_servers=5)
    assert not fleet.has_open_alert(MC, GATE_AUTO_REPLACE_WEDGED_ALERT)
    assert calls2["provision"]


def test_tick_min_interval_anchors_on_failed_attempt_row():
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))
    # A recent FAILED provision attempt: its row is not "live" (failed run) but its creation
    # still debounces the next attempt.
    control.create_deployment(CustomerDeployment(
        id=f"{BASE}-old", customer_name="failed", deployment_type="dedicated_server",
        environment="development", created_at=_ago(hours=1)))
    runs.create_run(ProvisioningRun(
        id="run1", account_id="acct", deployment_id=f"{BASE}-old", requested_by="t",
        status="dispatch_failed", created_at=_ago(hours=1)))
    decision, _opened, calls = _tick(control, fleet, runs, min_interval_seconds=21600)
    assert calls["provision"] == []               # debounced, even though the old row is dead
    assert "debounce" in decision.reason


def test_tick_recommends_reaping_a_superseded_dead_gate():
    control, fleet, runs = _stores()
    # A healthy NEW gate is designated; the old base gate is undesignated + dead.
    _designate_gate(control, gate_id=f"{BASE}-new")
    control.create_deployment(CustomerDeployment(
        id=BASE, customer_name=BASE, deployment_type="dedicated_server",
        environment="development", created_at=_ago(days=40)))
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=2))
    _decision, opened, calls = _tick(control, fleet, runs)
    assert calls["provision"] == [] and calls["designate"] == []   # new gate healthy: nothing to do
    assert any(a.kind == GATE_DECOMMISSION_RECOMMENDED_ALERT and a.deployment_id == BASE
               for a in opened)
    assert fleet.has_open_alert(BASE, GATE_DECOMMISSION_RECOMMENDED_ALERT)


def test_tick_resolves_reap_when_old_gate_recovers():
    control, fleet, runs = _stores()
    _designate_gate(control, gate_id=f"{BASE}-new")
    control.create_deployment(CustomerDeployment(
        id=BASE, customer_name=BASE, deployment_type="dedicated_server",
        environment="development", created_at=_ago(days=40)))
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=2))
    _tick(control, fleet, runs)
    assert fleet.has_open_alert(BASE, GATE_DECOMMISSION_RECOMMENDED_ALERT)
    # Old gate's box recovers -> its infra alert is resolved -> the reap recommendation clears.
    fleet.resolve_open_alerts(BASE, "missed_heartbeat", _iso(NOW))
    _tick(control, fleet, runs)
    assert not fleet.has_open_alert(BASE, GATE_DECOMMISSION_RECOMMENDED_ALERT)


def test_tick_wedges_on_pre_row_provision_failure_without_churn():
    # A provision rejected BEFORE a box is created (misconfigured MC) leaves no row to anchor the
    # min-interval debounce. The daemon must surface a STABLE wedge (degrade-to-alert), not spin
    # silently. Regression for the P2 review finding.
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))

    def _reject(email, region):
        raise RuntimeError("baseline images not in the registry allowlist")

    d1, opened1, _c1 = _tick(control, fleet, runs, provision=_reject)
    assert d1.action == "provision"
    assert any(a.kind == GATE_AUTO_REPLACE_WEDGED_ALERT for a in opened1)
    assert fleet.has_open_alert(MC, GATE_AUTO_REPLACE_WEDGED_ALERT)

    # Same failure next tick: the wedge is NOT re-opened (dedup -> no alert-row churn / webhook spam).
    _d2, opened2, _c2 = _tick(control, fleet, runs, provision=_reject)
    assert not any(a.kind == GATE_AUTO_REPLACE_WEDGED_ALERT for a in opened2)
    assert fleet.has_open_alert(MC, GATE_AUTO_REPLACE_WEDGED_ALERT)


def test_tick_gate_none_does_not_flap_reap_alerts():
    # When no gate is designated (transiently, mid un-designation) the reap sweep must leave existing
    # gate_decommission_recommended alerts alone rather than flapping them off and back on (P3).
    control, fleet, runs = _stores()
    control.create_deployment(CustomerDeployment(
        id=BASE, customer_name=BASE, deployment_type="dedicated_server", environment="development"))
    fleet.open_alert(FleetAlert(
        id="fa_reap", deployment_id=BASE, kind=GATE_DECOMMISSION_RECOMMENDED_ALERT, detail="x",
        status="open", created_at=_ago(hours=1)))
    _tick(control, fleet, runs)   # get_release_gate() is None -> no designated gate
    assert fleet.has_open_alert(BASE, GATE_DECOMMISSION_RECOMMENDED_ALERT)


def test_tick_never_touches_foreign_alerts_on_mc_row():
    control, fleet, runs = _stores()
    _designate_gate(control)
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))
    # Infra (heartbeat watchdog) + pipeline (pipeline watchdog) alerts share MC's row.
    _hard_alert(fleet, MC, "low_root_disk", _ago(hours=1))
    fleet.open_alert(FleetAlert(
        id="fa_stall", deployment_id=MC, kind=DEV_PIPELINE_STALLED_ALERT, detail="x",
        status="open", created_at=_ago(hours=1)))
    _tick(control, fleet, runs, max_fleet_servers=2)   # forces a wedge open on MC's row
    # The auto-replacer's own kind is managed; the other two are left strictly alone.
    assert fleet.has_open_alert(MC, GATE_AUTO_REPLACE_WEDGED_ALERT)
    assert fleet.has_open_alert(MC, "low_root_disk")
    assert fleet.has_open_alert(MC, DEV_PIPELINE_STALLED_ALERT)


def test_full_sequence_provision_wait_designate_recommend_reap():
    """The end-to-end world-derived loop: each tick re-derives state; provision/designate are
    stubbed, so the test simulates their real-world effects between ticks."""
    control, fleet, runs = _stores()
    _designate_gate(control, gate_id=BASE, region="nbg1")
    _hard_alert(fleet, BASE, "missed_heartbeat", _ago(hours=1))

    # Tick 1: dead gate, no replacement -> provision.
    d1, _o1, c1 = _tick(control, fleet, runs)
    assert d1.action == "provision" and c1["provision"] == [("admin@onebrain.test", "nbg1")]

    # (real provision would create the replacement box, still booting/unhealthy)
    replacement = f"{BASE}-r1"
    control.create_deployment(CustomerDeployment(
        id=replacement, customer_name="replacement", deployment_type="dedicated_server",
        environment="development", created_at=_iso(NOW)))

    # Tick 2: replacement in flight but not ready -> wait, never a second provision.
    d2, _o2, c2 = _tick(control, fleet, runs, blockers_for=lambda d: ["deployment_unhealthy"])
    assert d2.action == "noop" and c2["provision"] == []

    # Tick 3: replacement passes the blocker preflight -> designate.
    d3, _o3, c3 = _tick(control, fleet, runs, blockers_for=lambda d: [])
    assert d3.action == "designate" and c3["designate"] == [replacement]

    # (real designate would flip the gate marker to the replacement)
    control.designate_release_gate(replacement)

    # Tick 4: the new gate is healthy; the old one is undesignated + dead -> recommend reaping it.
    d4, _o4, c4 = _tick(control, fleet, runs)
    assert d4.action == "noop"
    assert c4["provision"] == [] and c4["designate"] == []
    assert fleet.has_open_alert(BASE, GATE_DECOMMISSION_RECOMMENDED_ALERT)


# --- gate_auto_replace_once gating (the daemon fast-path) ---------------------

def test_once_returns_empty_when_disabled():
    settings = SimpleNamespace(operator_mode=True, gate_auto_replace_enabled=False, deployment_id=MC)
    assert gate_auto_replace_once(settings, *_stores()) == []


def test_once_returns_empty_without_operator_mode():
    settings = SimpleNamespace(operator_mode=False, gate_auto_replace_enabled=True, deployment_id=MC)
    assert gate_auto_replace_once(settings, *_stores()) == []


def test_once_returns_empty_without_deployment_id():
    settings = SimpleNamespace(operator_mode=True, gate_auto_replace_enabled=True, deployment_id="")
    assert gate_auto_replace_once(settings, *_stores()) == []


def test_actor_is_self_identifying():
    assert GATE_AUTO_REPLACE_ACTOR == "mission-control:auto-gate-replace"

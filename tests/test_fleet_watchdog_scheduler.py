"""Mission Control's fleet alert scheduler."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.controlplane.base import CustomerDeployment
from app.controlplane.memory import MemoryControlPlaneStore
from app.fleet.base import Heartbeat
from app.fleet.heartbeat import CONTRACT_VERSION_V2
from app.fleet.memory import MemoryFleetStore
from app.fleet.watchdog_scheduler import start_fleet_watchdog, watchdog_once


def _settings(**overrides):
    values = {
        "operator_mode": True,
        "fleet_watchdog_seconds": 60,
        "fleet_missed_heartbeat_seconds": 600,
        "fleet_target_version": "",
        "fleet_low_root_disk_percent": 15,
        "fleet_low_data_disk_percent": 15,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_watchdog_once_opens_low_disk_alerts_for_registered_deployments():
    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id="dep_a", customer_name="Customer A", deployment_type="dedicated_server",
    ))
    fleet = MemoryFleetStore()
    now = datetime.now(timezone.utc).isoformat()
    fleet.record_heartbeat(Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION_V2, now, now, True,
        payload={"storage": {
            "root": {"total_bytes": 100, "available_bytes": 10},
            "data": {"total_bytes": 100, "available_bytes": 10},
        }},
    ))

    opened = watchdog_once(_settings(), control, fleet)

    assert {alert.kind for alert in opened} == {"low_root_disk", "low_data_disk"}


def test_watchdog_once_also_opens_pipeline_stall_alerts_on_mission_control():
    from datetime import timedelta

    from app.controlplane.base import ReleaseManifest, ReleasePromotion, ReleasePromotionEvent
    from app.fleet.base import DEV_PIPELINE_STALLED_ALERT

    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id="mc", customer_name="mc", deployment_type="dedicated_server"))
    stale = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    control.create_release_candidate(
        ReleaseManifest(version="2026.07.22.500", git_sha="a" * 40, modules={"onebrain-api": "1"}),
        ReleasePromotion(release_version="2026.07.22.500", state="dev_failed",
                         failure_reason="dev_rollout_failed", dev_completed_at=stale),
        ReleasePromotionEvent(id="", release_version="2026.07.22.500", action="seed",
                              to_state="dev_failed"),
    )
    fleet = MemoryFleetStore()
    settings = _settings(deployment_id="mc", pipeline_stall_alert_seconds=10800)

    opened = watchdog_once(settings, control, fleet)

    assert DEV_PIPELINE_STALLED_ALERT in {alert.kind for alert in opened}
    assert fleet.has_open_alert("mc", DEV_PIPELINE_STALLED_ALERT)


def test_self_deploy_alert_fires_at_the_pre_63_single_attempt_budget():
    # Regression (Codex #65): before the bounded self-deploy retry (#63) lands, MC gives up
    # after ONE failed self-deploy, so the stall alert must fire at 1 failure. The scheduler
    # defaults operator_self_max_attempts to 1 so it is not silent on that behavior.
    from app.controlplane.base import (
        ReleaseManifest, ReleasePromotion, ReleasePromotionEvent, RolloutRun,
    )
    from app.fleet.base import OPERATOR_SELF_DEPLOY_STALLED_ALERT

    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id="mc", customer_name="mc", deployment_type="dedicated_server",
        current_version="2026.07.22.400"))
    control.create_release_candidate(
        ReleaseManifest(version="2026.07.22.500", git_sha="a" * 40, modules={"onebrain-api": "1"}),
        ReleasePromotion(release_version="2026.07.22.500", state="dev_verified"),
        ReleasePromotionEvent(id="", release_version="2026.07.22.500", action="seed",
                              to_state="dev_verified"),
    )
    control._rollouts["roll_mc_1"] = RolloutRun(
        id="roll_mc_1", deployment_id="mc", target_version="2026.07.22.500",
        status="failed", started_by="operator-self:test")
    fleet = MemoryFleetStore()
    # auto-deploy on, and operator_self_max_attempts NOT set -> scheduler default of 1.
    settings = _settings(deployment_id="mc", operator_auto_deploy_enabled=True)

    opened = watchdog_once(settings, control, fleet)

    assert OPERATOR_SELF_DEPLOY_STALLED_ALERT in {alert.kind for alert in opened}


def test_watchdog_once_pushes_opened_alerts_to_the_webhook(monkeypatch):
    import app.fleet.alert_notify as alert_notify

    pushed: list = []
    monkeypatch.setattr(alert_notify, "push_open_alerts",
                        lambda url, alerts, **kw: pushed.append((url, [a.kind for a in alerts])))
    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id="dep_a", customer_name="A", deployment_type="dedicated_server"))
    fleet = MemoryFleetStore()   # no heartbeat -> a missed_heartbeat alert opens

    opened = watchdog_once(_settings(fleet_alert_webhook_url="https://hook.example/x"), control, fleet)

    assert any(alert.kind == "missed_heartbeat" for alert in opened)
    assert pushed and pushed[0][0] == "https://hook.example/x"
    assert "missed_heartbeat" in pushed[0][1]


def test_watchdog_once_does_not_push_without_a_webhook_url(monkeypatch):
    import app.fleet.alert_notify as alert_notify

    called: list = []
    monkeypatch.setattr(alert_notify, "push_open_alerts", lambda *a, **kw: called.append(True))
    control = MemoryControlPlaneStore()
    control.create_deployment(CustomerDeployment(
        id="dep_a", customer_name="A", deployment_type="dedicated_server"))
    fleet = MemoryFleetStore()

    watchdog_once(_settings(), control, fleet)   # no webhook url configured

    assert called == []


def test_start_fleet_watchdog_requires_operator_mode_and_positive_interval():
    assert start_fleet_watchdog(_settings(operator_mode=False)) is False
    assert start_fleet_watchdog(_settings(fleet_watchdog_seconds=0)) is False

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


def test_start_fleet_watchdog_requires_operator_mode_and_positive_interval():
    assert start_fleet_watchdog(_settings(operator_mode=False)) is False
    assert start_fleet_watchdog(_settings(fleet_watchdog_seconds=0)) is False

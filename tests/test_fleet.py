"""Mission Control fleet control plane: heartbeat contract, store, watchdog,
ingest/overview router, and the reporter payload builder + sender.

Everything here runs against the in-memory fleet store and calls the router
functions directly with an injected store (the idiom used by test_controlplane),
so no database or network is touched.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routers.fleet as fleet_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.controlplane.base import CustomerDeployment
from app.controlplane.memory import MemoryControlPlaneStore
from app.fleet.base import FleetAlert, FleetKey, Heartbeat
from app.fleet.heartbeat import (
    CONTRACT_VERSION, CONTRACT_VERSION_V2, AnyFleetHeartbeat, FleetHeartbeat,
    FleetHeartbeatV2, StorageCapacityReport, StorageReport, UpdateReport,
    build_heartbeat, build_heartbeat_v2,
)
from app.fleet.keys import generate_fleet_key, hash_secret, parse_fleet_key, verify_secret
from app.fleet.memory import MemoryFleetStore
from app.fleet.reporter import report_once, send_heartbeat, start_reporter
from app.fleet.watchdog import desired_alerts, run_watchdog


# --- helpers -----------------------------------------------------------------

def _principal(role_id: str = "admin") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@onebrain",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
    )


def _minted_key(store: MemoryFleetStore, deployment_id: str = "dep_a") -> str:
    key_id, secret, token = generate_fleet_key()
    store.create_key(FleetKey(id=key_id, key_hash=hash_secret(secret),
                              deployment_id=deployment_id, created_at="2026-07-11T00:00:00+00:00"))
    return token


def _control_with(deployment_id: str = "dep_a") -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id=deployment_id, customer_name="Customer A", deployment_type="dedicated_server",
        release_ring="pilot", current_version="2026.07.0",
    ))
    return store


def _heartbeat_body(deployment_id: str = "dep_a", *, healthy: bool = True, version: str = "2026.07.0",
                    reported_at: str = "") -> FleetHeartbeat:
    # Default to a near-now reported_at so the ingest skew guard accepts it (the
    # real reporter stamps its own current time); callers can pin it explicitly.
    from datetime import datetime, timezone

    return build_heartbeat(
        deployment_id=deployment_id, reported_at=reported_at or datetime.now(timezone.utc).isoformat(),
        version=version, migration_revision="0015_fleet_telemetry",
        onebrain_healthy=healthy, chunks=12, users=3, accounts=1,
    )


# --- heartbeat contract ------------------------------------------------------

def test_heartbeat_schema_is_closed_and_rejects_unknown_fields():
    body = _heartbeat_body()
    assert body.contract_version == CONTRACT_VERSION
    assert body.healthy is True
    # extra="forbid" — an out-of-contract field (a smuggled name/email/text) is rejected.
    with pytest.raises(ValidationError):
        FleetHeartbeat(
            deployment_id="dep_a", reported_at="t", onebrain=body.onebrain,
            customer_email="leak@example.com",
        )


def test_heartbeat_health_rolls_up_module_health():
    from app.fleet.heartbeat import ModuleReport

    hb = build_heartbeat(deployment_id="dep_a", reported_at="t", onebrain_healthy=True,
                         modules=[ModuleReport(module_id="communication-api", healthy=False)])
    assert hb.healthy is False  # one unhealthy module makes the whole heartbeat unhealthy


def test_heartbeat_counts_reject_negative():
    with pytest.raises(ValidationError):
        build_heartbeat(deployment_id="dep_a", reported_at="t", chunks=-1)


def test_storage_capacity_is_metadata_only_and_bounds_available_space():
    storage = StorageReport(
        root=StorageCapacityReport(total_bytes=100, available_bytes=20),
        data=StorageCapacityReport(total_bytes=200, available_bytes=40),
    )
    hb = build_heartbeat_v2(deployment_id="dep_a", reported_at="t", storage=storage)
    assert hb.storage.root.available_bytes == 20
    assert hb.storage.data_volume_unavailable is False
    with pytest.raises(ValidationError):
        StorageCapacityReport(total_bytes=100, available_bytes=101)


# --- fleet keys --------------------------------------------------------------

def test_fleet_key_roundtrip_and_hash():
    key_id, secret, token = generate_fleet_key()
    assert token == f"fk_{key_id}_{secret}"
    assert parse_fleet_key(token) == (key_id, secret)
    assert verify_secret(secret, hash_secret(secret))
    assert not verify_secret("wrong", hash_secret(secret))


@pytest.mark.parametrize("bad", ["", "nope", "sk_abc_def", "fk_only", "fk__nosecret", "fk_id_"])
def test_parse_fleet_key_rejects_malformed(bad):
    assert parse_fleet_key(bad) is None


# --- memory store ------------------------------------------------------------

def test_memory_store_key_lifecycle():
    store = MemoryFleetStore()
    key_id, secret, _ = generate_fleet_key()
    store.create_key(FleetKey(id=key_id, key_hash=hash_secret(secret), deployment_id="dep_a"))

    assert store.get_key(key_id).status == "active"
    assert [k.id for k in store.list_keys("dep_a")] == [key_id]
    assert store.list_keys("other") == []

    store.touch_key(key_id, "2026-07-11T01:00:00+00:00")
    assert store.get_key(key_id).last_used_at == "2026-07-11T01:00:00+00:00"

    assert store.revoke_key(key_id) is True
    assert store.get_key(key_id).status == "revoked"
    assert store.revoke_key(key_id) is False  # already revoked


def test_memory_store_keeps_latest_heartbeat_per_deployment():
    store = MemoryFleetStore()
    store.record_heartbeat(Heartbeat("hb1", "dep_a", CONTRACT_VERSION, "t1", "2026-07-11T00:00:00+00:00", True))
    store.record_heartbeat(Heartbeat("hb2", "dep_a", CONTRACT_VERSION, "t2", "2026-07-11T00:01:00+00:00", False))
    store.record_heartbeat(Heartbeat("hb3", "dep_b", CONTRACT_VERSION, "t3", "2026-07-11T00:00:30+00:00", True))

    assert store.latest_heartbeat("dep_a").id == "hb2"
    latest = store.latest_heartbeats()
    assert set(latest) == {"dep_a", "dep_b"}
    assert latest["dep_a"].healthy is False


def test_memory_store_alert_open_resolve_and_has():
    store = MemoryFleetStore()
    store.open_alert(FleetAlert("al1", "dep_a", "missed_heartbeat", "gone", created_at="t"))
    assert store.has_open_alert("dep_a", "missed_heartbeat") is True
    assert [a.id for a in store.list_open_alerts("dep_a")] == ["al1"]

    resolved = store.resolve_open_alerts("dep_a", "missed_heartbeat", "t2")
    assert resolved == 1
    assert store.has_open_alert("dep_a", "missed_heartbeat") is False
    assert store.list_open_alerts("dep_a") == []


def test_memory_store_json_persistence(tmp_path):
    path = str(tmp_path / "fleet.json")
    s1 = MemoryFleetStore(persist_path=path)
    key_id, secret, _ = generate_fleet_key()
    s1.create_key(FleetKey(id=key_id, key_hash=hash_secret(secret), deployment_id="dep_a"))
    s1.record_heartbeat(Heartbeat("hb1", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:00+00:00", True))
    s1.open_alert(FleetAlert("al1", "dep_a", "unhealthy", created_at="t"))

    s2 = MemoryFleetStore(persist_path=path)  # reloads from disk
    assert s2.get_key(key_id) is not None
    assert s2.latest_heartbeat("dep_a").id == "hb1"
    assert s2.has_open_alert("dep_a", "unhealthy") is True


# --- watchdog (pure desired-state) -------------------------------------------

def test_desired_alerts_flags_missing_heartbeat():
    assert desired_alerts(heartbeat=None, now_iso="2026-07-11T00:10:00+00:00",
                          missed_after_seconds=600) == {"missed_heartbeat": "no heartbeat received yet"}


def test_desired_alerts_flags_stale_heartbeat():
    hb = Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:00+00:00", True)
    want = desired_alerts(heartbeat=hb, now_iso="2026-07-11T00:20:00+00:00", missed_after_seconds=600)
    assert "missed_heartbeat" in want  # 1200s > 600s threshold


def test_desired_alerts_unhealthy_only_when_still_reporting():
    fresh = Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:30+00:00", False)
    want = desired_alerts(heartbeat=fresh, now_iso="2026-07-11T00:01:00+00:00", missed_after_seconds=600)
    assert want == {"unhealthy": "deployment reported unhealthy"}

    stale = Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:00+00:00", False)
    want2 = desired_alerts(heartbeat=stale, now_iso="2026-07-11T00:20:00+00:00", missed_after_seconds=600)
    # gone silent AND unhealthy -> missed_heartbeat leads, no separate unhealthy alert
    assert "missed_heartbeat" in want2 and "unhealthy" not in want2


def test_desired_alerts_flags_version_drift():
    hb = Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:30+00:00", True, version="2026.06.0")
    want = desired_alerts(heartbeat=hb, now_iso="2026-07-11T00:01:00+00:00",
                          missed_after_seconds=600, expected_version="2026.07.0")
    assert "version_drift" in want


def test_desired_alerts_healthy_current_deployment_has_none():
    hb = Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:30+00:00", True, version="2026.07.0")
    assert desired_alerts(heartbeat=hb, now_iso="2026-07-11T00:01:00+00:00",
                          missed_after_seconds=600, expected_version="2026.07.0") == {}


def test_desired_alerts_flags_known_low_root_and_data_capacity():
    hb = Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:00:30+00:00", True,
        payload={
            "storage": {
                "root": {"total_bytes": 100, "available_bytes": 10},
                "data": {"total_bytes": 100, "available_bytes": 20},
            },
        },
    )
    assert desired_alerts(
        heartbeat=hb, now_iso="2026-07-11T00:01:00+00:00", missed_after_seconds=600,
        low_root_disk_percent=15, low_data_disk_percent=25,
    ) == {
        "low_root_disk": "root disk has 10.0% free (threshold 15%)",
        "low_data_disk": "data disk has 20.0% free (threshold 25%)",
    }


def test_data_volume_unavailable_is_a_concrete_watchdog_alert():
    hb = Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:00:30+00:00", True,
        payload={"storage": {
            "root": {"total_bytes": 100, "available_bytes": 50},
            "data": {"total_bytes": 0, "available_bytes": 0},
            "data_volume_unavailable": True,
        }},
    )

    assert desired_alerts(
        heartbeat=hb, now_iso="2026-07-11T00:01:00+00:00", missed_after_seconds=600,
    ) == {
        "data_volume_unavailable": "persistent data volume is unavailable or failed verification",
    }


def test_run_watchdog_resolves_low_disk_alert_after_capacity_recovers():
    store = MemoryFleetStore()
    store.record_heartbeat(Heartbeat(
        "low", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:00:00+00:00", True,
        payload={"storage": {
            "root": {"total_bytes": 100, "available_bytes": 10},
            "data": {"total_bytes": 100, "available_bytes": 10},
        }},
    ))
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"al_{counter['n']}"

    opened = run_watchdog(
        store, ["dep_a"], now_iso="2026-07-11T00:00:30+00:00", missed_after_seconds=600,
        low_root_disk_percent=15, low_data_disk_percent=15, next_id=next_id,
    )
    assert {alert.kind for alert in opened} == {"low_root_disk", "low_data_disk"}

    # A legacy/partially upgraded reporter can omit storage (or report 0/0).
    # That does not prove the disk recovered, so the previous alerts remain
    # open until the next *known healthy* capacity report.
    store.record_heartbeat(Heartbeat(
        "unknown", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:00:45+00:00", True,
        payload={"storage": {
            "root": {"total_bytes": 0, "available_bytes": 0},
            "data": {"total_bytes": 0, "available_bytes": 0},
        }},
    ))
    assert run_watchdog(
        store, ["dep_a"], now_iso="2026-07-11T00:00:50+00:00", missed_after_seconds=600,
        low_root_disk_percent=15, low_data_disk_percent=15, next_id=next_id,
    ) == []
    assert {alert.kind for alert in store.list_open_alerts("dep_a")} == {
        "low_root_disk", "low_data_disk",
    }

    store.record_heartbeat(Heartbeat(
        "recovered", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:01:00+00:00", True,
        payload={"storage": {
            "root": {"total_bytes": 100, "available_bytes": 60},
            "data": {"total_bytes": 100, "available_bytes": 60},
        }},
    ))
    assert run_watchdog(
        store, ["dep_a"], now_iso="2026-07-11T00:01:30+00:00", missed_after_seconds=600,
        low_root_disk_percent=15, low_data_disk_percent=15, next_id=next_id,
    ) == []
    assert store.list_open_alerts("dep_a") == []


def test_watchdog_requires_an_explicit_recovered_data_volume_signal_to_resolve():
    store = MemoryFleetStore()
    store.record_heartbeat(Heartbeat(
        "unavailable", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:00:00+00:00", True,
        payload={"storage": {"data_volume_unavailable": True}},
    ))
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"al_{counter['n']}"

    opened = run_watchdog(
        store, ["dep_a"], now_iso="2026-07-11T00:00:30+00:00", missed_after_seconds=600,
        next_id=next_id,
    )
    assert [alert.kind for alert in opened] == ["data_volume_unavailable"]

    store.record_heartbeat(Heartbeat(
        "legacy", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:00:45+00:00", True,
        payload={"storage": {"data": {"total_bytes": 0, "available_bytes": 0}}},
    ))
    assert run_watchdog(
        store, ["dep_a"], now_iso="2026-07-11T00:00:50+00:00", missed_after_seconds=600,
        next_id=next_id,
    ) == []
    assert store.has_open_alert("dep_a", "data_volume_unavailable")

    store.record_heartbeat(Heartbeat(
        "verified", "dep_a", CONTRACT_VERSION_V2, "t", "2026-07-11T00:01:00+00:00", True,
        payload={"storage": {"data_volume_unavailable": False}},
    ))
    assert run_watchdog(
        store, ["dep_a"], now_iso="2026-07-11T00:01:30+00:00", missed_after_seconds=600,
        next_id=next_id,
    ) == []
    assert not store.has_open_alert("dep_a", "data_volume_unavailable")


# --- watchdog reconciler -----------------------------------------------------

def test_run_watchdog_opens_and_resolves_against_store():
    store = MemoryFleetStore()
    store.record_heartbeat(Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:00+00:00", True))
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"al_{counter['n']}"

    # dep_a is stale (20min > 10min); dep_b never reported.
    opened = run_watchdog(store, ["dep_a", "dep_b"], now_iso="2026-07-11T00:20:00+00:00",
                          missed_after_seconds=600, next_id=next_id)
    assert {a.deployment_id for a in opened} == {"dep_a", "dep_b"}
    assert store.has_open_alert("dep_a", "missed_heartbeat")

    # A fresh heartbeat for dep_a arrives; re-running resolves its alert and opens none new.
    store.record_heartbeat(Heartbeat("hb2", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:25:00+00:00", True))
    opened2 = run_watchdog(store, ["dep_a"], now_iso="2026-07-11T00:25:30+00:00",
                           missed_after_seconds=600, next_id=next_id)
    assert opened2 == []
    assert store.has_open_alert("dep_a", "missed_heartbeat") is False


def test_run_watchdog_is_idempotent():
    store = MemoryFleetStore()
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"al_{counter['n']}"

    args = dict(now_iso="2026-07-11T00:20:00+00:00", missed_after_seconds=600, next_id=next_id)
    run_watchdog(store, ["dep_a"], **args)
    second = run_watchdog(store, ["dep_a"], **args)
    assert second == []  # the alert already exists; no duplicate opened
    assert len(store.list_open_alerts("dep_a")) == 1


# --- router: heartbeat ingest ------------------------------------------------

def test_ingest_heartbeat_records_and_touches_key(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    ack = fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}")

    assert ack.received is True and ack.deployment_id == "dep_a"
    assert store.latest_heartbeat("dep_a") is not None
    assert store.latest_heartbeat("dep_a").version == "2026.07.0"
    key_id = parse_fleet_key(token)[0]
    assert store.get_key(key_id).last_used_at != ""
    assert store.get_key(key_id).last_used_at == store.latest_heartbeat("dep_a").received_at


def test_ingest_heartbeat_rejects_missing_and_bad_keys(monkeypatch):
    store = MemoryFleetStore()
    _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    for bad_auth in ["", "Bearer", "Bearer fk_nope_nope"]:
        with pytest.raises(HTTPException) as ei:
            fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=bad_auth)
        assert ei.value.status_code == 401


def test_ingest_heartbeat_key_cannot_report_for_another_deployment(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    with pytest.raises(HTTPException) as ei:
        fleet_router.ingest_heartbeat(_heartbeat_body("dep_b"), authorization=f"Bearer {token}")
    assert ei.value.status_code == 403


def test_ingest_heartbeat_rejects_revoked_key(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    store.revoke_key(parse_fleet_key(token)[0])
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    with pytest.raises(HTTPException) as ei:
        fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}")
    assert ei.value.status_code == 401


# --- router: key management (operator-admin) ---------------------------------

def test_mint_fleet_key_requires_known_deployment_and_returns_token_once(monkeypatch):
    store = MemoryFleetStore()
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    minted = fleet_router.mint_fleet_key(
        fleet_router.FleetKeyCreate(deployment_id="dep_a", label="railway"), principal=_principal("admin"))
    assert minted.token.startswith("fk_")
    key_id = parse_fleet_key(minted.token)[0]
    assert store.get_key(key_id) is not None  # persisted (hash only)
    assert store.get_key(key_id).key_hash != minted.token  # never store the plaintext


def test_mint_fleet_key_rejects_unknown_deployment(monkeypatch):
    store = MemoryFleetStore()
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    with pytest.raises(HTTPException) as ei:
        fleet_router.mint_fleet_key(
            fleet_router.FleetKeyCreate(deployment_id="ghost"), principal=_principal("admin"))
    assert ei.value.status_code == 404


def test_key_management_is_operator_admin_only(monkeypatch):
    store = MemoryFleetStore()
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    non_admin = _principal("front_desk")

    with pytest.raises(HTTPException) as ei:
        fleet_router.mint_fleet_key(fleet_router.FleetKeyCreate(deployment_id="dep_a"), principal=non_admin)
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException):
        fleet_router.list_fleet_keys(principal=non_admin)
    with pytest.raises(HTTPException):
        fleet_router.revoke_fleet_key("whatever", principal=non_admin)
    with pytest.raises(HTTPException):
        fleet_router.fleet_overview(principal=non_admin)


def test_list_and_revoke_fleet_keys(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    key_id = parse_fleet_key(token)[0]
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    listed = fleet_router.list_fleet_keys(principal=_principal("admin"))
    assert [k.id for k in listed] == [key_id]

    result = fleet_router.revoke_fleet_key(key_id, principal=_principal("admin"))
    assert result == {"revoked": key_id}
    with pytest.raises(HTTPException) as ei:
        fleet_router.revoke_fleet_key(key_id, principal=_principal("admin"))  # already revoked
    assert ei.value.status_code == 404


# --- router: overview --------------------------------------------------------

def test_fleet_overview_joins_registry_heartbeats_and_alerts(monkeypatch):
    fleet = MemoryFleetStore()
    fleet.record_heartbeat(Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION, "2026-07-11T00:00:00+00:00", "2026-07-11T00:00:01+00:00",
        True, version="2026.07.0", migration_revision="0015_fleet_telemetry",
        payload=_heartbeat_body("dep_a").model_dump()))
    fleet.open_alert(FleetAlert("al", "dep_a", "version_drift", created_at="t"))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    out = fleet_router.fleet_overview(principal=_principal("admin"))

    assert out.total == 1 and out.healthy == 1 and out.with_open_alerts == 1
    row = out.deployments[0]
    assert row.deployment_id == "dep_a"
    assert row.reported_version == "2026.07.0"
    assert row.migration_revision == "0015_fleet_telemetry"
    assert row.open_alerts == ["version_drift"]
    assert row.counts["chunks"] == 12 and row.counts["users"] == 3


def test_fleet_overview_handles_deployment_without_heartbeat(monkeypatch):
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: MemoryFleetStore())
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    out = fleet_router.fleet_overview(principal=_principal("admin"))
    assert out.total == 1 and out.healthy == 0
    assert out.deployments[0].healthy is None
    assert out.deployments[0].counts == {}


def test_fleet_overview_exposes_reported_root_and_data_capacity(monkeypatch):
    fleet = MemoryFleetStore()
    body = build_heartbeat_v2(
        deployment_id="dep_a", reported_at="2026-07-11T00:00:00+00:00",
        storage=StorageReport(
            root=StorageCapacityReport(total_bytes=1000, available_bytes=200),
            data=StorageCapacityReport(total_bytes=2000, available_bytes=800),
        ),
    )
    fleet.record_heartbeat(Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION_V2, body.reported_at, "2026-07-11T00:00:01+00:00",
        True, payload=body.model_dump(),
    ))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    row = fleet_router.fleet_overview(principal=_principal("admin")).deployments[0]
    assert row.storage.root.available_bytes == 200
    assert row.storage.data.total_bytes == 2000


def test_fleet_overview_derives_https_login_url(monkeypatch):
    from types import SimpleNamespace

    control = MemoryControlPlaneStore()
    for dep in ("nft_gym", "development-gate"):
        control.create_deployment(CustomerDeployment(id=dep, customer_name=dep, release_ring="pilot"))
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: MemoryFleetStore())
    # DNS-enabled fleet: boxes are served over HTTPS at <label>.<base_domain>.
    monkeypatch.setattr(fleet_router, "get_settings", lambda: SimpleNamespace(
        fleet_dns_provider="hetzner", fleet_base_domain="onlyonebrain.com", fleet_dns_zone_id="zone1"))

    rows = {r.deployment_id: r for r in fleet_router.fleet_overview(principal=_principal("admin")).deployments}
    # Derived from the deployment id (not the mutable external_run_url); underscores
    # become RFC-1123 dashes to match the box's real TLS hostname.
    assert rows["nft_gym"].login_url == "https://nft-gym.onlyonebrain.com"
    assert rows["development-gate"].login_url == "https://development-gate.onlyonebrain.com"


def test_fleet_overview_suppresses_login_url_for_ip_only_fleet(monkeypatch):
    from types import SimpleNamespace

    # No DNS provider/zone -> boxes serve plain HTTP on the raw IP. An http:// link
    # can't hold a secure-cookie session, so the overview exposes no login link.
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: MemoryFleetStore())
    monkeypatch.setattr(fleet_router, "get_settings", lambda: SimpleNamespace(
        fleet_dns_provider="", fleet_base_domain="", fleet_dns_zone_id=""))

    out = fleet_router.fleet_overview(principal=_principal("admin"))
    assert out.deployments[0].login_url == ""


# --- reporter ----------------------------------------------------------------

def test_collect_storage_report_keeps_data_volume_distinct_from_root(monkeypatch):
    from app.fleet import reporter

    observed = []

    def capacity(path):
        observed.append(path)
        return StorageCapacityReport(total_bytes=1000, available_bytes=100 if path == "/" else 300)

    monkeypatch.setattr(reporter, "_storage_capacity", capacity)
    monkeypatch.setattr(reporter.os.path, "ismount", lambda _path: True)
    storage = reporter.collect_storage_report("/mnt/onebrain-data")

    assert observed == ["/", "/mnt/onebrain-data"]
    assert storage.root.available_bytes == 100
    assert storage.data.available_bytes == 300
    assert storage.data_volume_unavailable is False

class _FakeResponse:
    def __init__(self, status: int):
        self._status = status
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self._status


def test_send_heartbeat_posts_bearer_and_returns_status():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.data
        return _FakeResponse(200)

    status = send_heartbeat("https://mc.example.com/", "fk_a_b", _heartbeat_body("dep_a"), opener=opener)

    assert status == 200
    assert captured["url"] == "https://mc.example.com/api/fleet/heartbeat"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer fk_a_b"
    assert b'"deployment_id":"dep_a"' in captured["body"].replace(b" ", b"")


def test_report_once_skips_when_unconfigured():
    from app.config import Settings

    settings = Settings(fleet_url="", fleet_key="", deployment_id="")
    assert report_once(settings) is False


def test_reporter_is_disabled_by_explicit_customer_flag(monkeypatch):
    """A customer-shaped box must not report even if a test/leaked env supplied
    otherwise valid fleet coordinates. Its host-only gate agent owns reporting."""
    from app.config import Settings

    settings = Settings(
        fleet_url="https://mc", fleet_key="fk_a_b", deployment_id="dep_a",
        fleet_reporter_enabled=False,
    )
    monkeypatch.setattr("app.fleet.reporter.collect_heartbeat", lambda s: pytest.fail("must not collect"))
    assert report_once(settings) is False
    assert start_reporter(settings) is False


def test_report_once_swallows_send_errors(monkeypatch):
    from app.config import Settings

    settings = Settings(fleet_url="https://mc", fleet_key="fk_a_b", deployment_id="dep_a")
    monkeypatch.setattr("app.fleet.reporter.collect_heartbeat", lambda s: _heartbeat_body("dep_a"))

    def boom(request, timeout):
        raise OSError("connection refused")

    # Must not raise — a reporting failure can never disturb the serving deployment.
    assert report_once(settings, opener=boom) is False


def test_report_once_returns_true_on_success(monkeypatch):
    from app.config import Settings

    settings = Settings(fleet_url="https://mc", fleet_key="fk_a_b", deployment_id="dep_a")
    monkeypatch.setattr("app.fleet.reporter.collect_heartbeat", lambda s: _heartbeat_body("dep_a"))
    assert report_once(settings, opener=lambda req, t: _FakeResponse(200)) is True


def test_report_once_treats_http_error_as_failure(monkeypatch):
    from app.config import Settings

    settings = Settings(fleet_url="https://mc", fleet_key="fk_a_b", deployment_id="dep_a")
    monkeypatch.setattr("app.fleet.reporter.collect_heartbeat", lambda s: _heartbeat_body("dep_a"))
    assert report_once(settings, opener=lambda req, t: _FakeResponse(401)) is False


def test_collect_heartbeat_builds_metadata_only_payload():
    from app.config import Settings
    from app.fleet.reporter import collect_heartbeat

    hb = collect_heartbeat(Settings(deployment_id="dep_local"))

    assert hb.contract_version == CONTRACT_VERSION_V2
    assert hb.deployment_id == "dep_local"
    assert hb.onebrain.version == "0.1.0"  # build_version unset -> app.__version__
    # Memory mode: no schema to attest — claim nothing; computed health holds.
    assert hb.onebrain.migration_revision == ""
    assert hb.onebrain.healthy is True
    # Everything in the payload is a count/flag/version/enum — no free-text customer content.
    payload = hb.model_dump()
    assert set(payload) == {"contract_version", "deployment_id", "reported_at", "onebrain", "modules", "update", "storage"}
    assert payload["update"]["outcome"] == "none"
    assert set(payload["storage"]) == {"root", "data", "data_volume_unavailable"}


# --- ground-truth reporter (fleet.v2 emitter) ---------------------------------

def _patch_cheap_stores(monkeypatch):
    """Replace every store getter collect_heartbeat consults with a working
    fake so pgvector-mode tests never build a real Postgres-backed store."""
    from types import SimpleNamespace

    counting = SimpleNamespace(count=lambda: 1)
    monkeypatch.setattr("app.deps.get_store", lambda: counting)
    monkeypatch.setattr("app.deps.get_intake_store", lambda: counting)
    monkeypatch.setattr("app.deps.get_user_store", lambda: counting)
    monkeypatch.setattr("app.deps.get_platform_store",
                        lambda: SimpleNamespace(list_accounts=lambda: []))
    monkeypatch.setattr("app.deps.get_service_key_store",
                        lambda: SimpleNamespace(summary=lambda: SimpleNamespace(active=0)))
    monkeypatch.setattr(
        "app.deps.get_job_store",
        lambda: SimpleNamespace(summary=lambda recent_failures_limit=0: SimpleNamespace(by_status={})))


def test_collect_reports_build_version_when_set():
    from app.config import Settings
    from app.fleet.reporter import collect_heartbeat

    hb = collect_heartbeat(Settings(deployment_id="dep_local", build_version="2026.07.2"))
    assert hb.onebrain.version == "2026.07.2"  # CI-stamped version wins over __version__


def test_collect_survives_store_failure_and_degrades_healthy(monkeypatch):
    from app.config import Settings
    from app.fleet.reporter import collect_heartbeat

    def boom():
        raise RuntimeError("store down")

    # collect_heartbeat imports the getter from app.deps at call time.
    monkeypatch.setattr("app.deps.get_store", boom)

    hb = collect_heartbeat(Settings(deployment_id="dep_local"))  # must not raise
    assert hb.onebrain.chunks == 0  # degraded field, not a dead beat
    assert hb.onebrain.healthy is False  # a failing collector flips computed health


def test_collect_pgvector_revision_mismatch_marks_unhealthy(monkeypatch):
    from app.config import Settings
    from app.db.schema import REQUIRED_ALEMBIC_REVISION
    from app.fleet.reporter import collect_heartbeat

    _patch_cheap_stores(monkeypatch)
    # A1 — construction trap: pg_database_url is a read-only derived @property
    # over the real field database_url; a pg_database_url= kwarg is SILENTLY
    # dropped (extra="ignore") leaving the DSN "". The DSN must also name a
    # test database or the pytest DSN guard raises inside _safe, masking the
    # matching-revision companion case as a confusing failure.
    settings = Settings(vector_store="pgvector", database_url="postgresql://x/test_y",
                        deployment_id="dep_pg")

    monkeypatch.setattr("app.fleet.reporter.read_live_alembic_revision", lambda dsn: "0001_baseline")
    hb = collect_heartbeat(settings)
    assert hb.onebrain.migration_revision == "0001_baseline"  # live read, not the constant
    assert hb.onebrain.healthy is False

    monkeypatch.setattr("app.fleet.reporter.read_live_alembic_revision",
                        lambda dsn: REQUIRED_ALEMBIC_REVISION)
    hb2 = collect_heartbeat(settings)
    assert hb2.onebrain.migration_revision == REQUIRED_ALEMBIC_REVISION
    assert hb2.onebrain.healthy is True

    def boom(dsn):
        raise OSError("db unreachable")

    monkeypatch.setattr("app.fleet.reporter.read_live_alembic_revision", boom)
    hb3 = collect_heartbeat(settings)
    assert hb3.onebrain.migration_revision == ""
    assert hb3.onebrain.healthy is False


def test_collect_reads_update_state_file(tmp_path):
    import json
    from app.config import Settings
    from app.fleet.reporter import collect_heartbeat

    state = {"last_target_version": "2026.07.2", "outcome": "succeeded",
             "migration_reached": "0019_trust_primitives", "attempt_id": "ro_42",
             "ts": "2026-07-12T00:00:00+00:00"}
    (tmp_path / "update_state.json").write_text(json.dumps(state), encoding="utf-8")

    hb = collect_heartbeat(Settings(deployment_id="dep_local", data_dir=str(tmp_path)))
    payload = hb.model_dump()
    assert payload["update"]["outcome"] == "succeeded"
    assert payload["update"]["attempt_id"] == "ro_42"

    (tmp_path / "update_state.json").write_text("{not json", encoding="utf-8")
    hb2 = collect_heartbeat(Settings(deployment_id="dep_local", data_dir=str(tmp_path)))
    assert hb2.model_dump()["update"]["outcome"] == "none"  # corrupt file: default, no raise


def test_module_probes_off_by_default():
    from app.config import Settings
    from app.fleet.reporter import collect_heartbeat

    hb = collect_heartbeat(Settings(deployment_id="dep_local"))
    assert hb.modules == []  # no probes, no module claims (Railway stays as today)


def test_module_probes_collect_reports():
    from app.config import Settings
    from app.fleet.module_probe import collect_module_reports

    settings = Settings(
        deployment_id="dep_local", module_probes_enabled=True,
        local_modules="communication-api,onebrain-workers,communication-workers")

    def opener(request, timeout):
        if "communication-api:4000" in request.full_url:
            return _FakeResponse(200)
        if "communication-workers:4200" in request.full_url:
            raise ConnectionRefusedError("refused")
        raise AssertionError(f"unexpected probe: {request.full_url}")

    reports = {r.module_id: r for r in collect_module_reports(settings, opener=opener)}
    # onebrain-workers (kind 'none') is absent: no listener means no claim, not a fabricated one.
    assert set(reports) == {"communication-api", "communication-workers"}
    assert reports["communication-api"].healthy is True
    # comm-workers' liveness listener is fail-open on connection refused (comm's own policy).
    assert reports["communication-workers"].healthy is True

    degraded = collect_module_reports(
        Settings(deployment_id="dep_local", module_probes_enabled=True,
                 local_modules="communication-api"),
        opener=lambda request, timeout: _FakeResponse(503))
    assert [r.module_id for r in degraded] == ["communication-api"]
    assert degraded[0].healthy is False  # 5xx from the module: alive but unhealthy


def test_module_probe_http_error_is_live_listener_and_closed():
    """A >=400 answer arrives as a raised urllib HTTPError: it IS a live
    listener (healthy = status < 500), and the error's live response body
    must be closed rather than leaked."""
    import urllib.error

    from app.fleet.module_probe import probe_module
    from app.module_manifest import HealthProbe

    class _TrackedHTTPError(urllib.error.HTTPError):
        def __init__(self, code):
            super().__init__("http://communication-api:4000/health", code, "err", None, None)
            self.was_closed = False

        def close(self):
            self.was_closed = True
            super().close()

    probe = HealthProbe(module_id="communication-api", kind="http", port=4000, path="/health")

    err = _TrackedHTTPError(404)

    def opener_404(request, timeout):
        raise err

    report = probe_module(probe, opener=opener_404)
    assert report.healthy is True  # 404 = a live listener answering
    assert err.was_closed

    err = _TrackedHTTPError(503)

    def opener_503(request, timeout):
        raise err

    report = probe_module(probe, opener=opener_503)
    assert report.healthy is False  # 5xx = alive but unhealthy
    assert err.was_closed


def test_report_once_posts_v2():
    import json
    from app.config import Settings

    captured = {}

    def opener(request, timeout):
        captured["body"] = request.data
        return _FakeResponse(200)

    settings = Settings(fleet_url="https://mc", fleet_key="fk_a_b", deployment_id="dep_a")
    assert report_once(settings, opener=opener) is True

    body = json.loads(captured["body"])
    assert body["contract_version"] == "fleet.v2"
    assert set(body) == {"contract_version", "deployment_id", "reported_at", "onebrain", "modules", "update", "storage"}


# --- heartbeat ingest hardening ---------------------------------------------

def test_ingest_heartbeat_rejects_skewed_reported_at(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    stale = _heartbeat_body("dep_a", reported_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(HTTPException) as ei:
        fleet_router.ingest_heartbeat(stale, authorization=f"Bearer {token}")
    assert ei.value.status_code == 400


def test_ingest_heartbeat_rate_limited_per_deployment(monkeypatch):
    from app.auth.throttle import RateLimiter

    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    # One shared limiter of 2/window so the third post in the window is rejected.
    limiter = RateLimiter(2, 60)
    monkeypatch.setattr(fleet_router, "get_fleet_heartbeat_rate_limiter", lambda: limiter)

    assert fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}").received
    assert fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}").received
    with pytest.raises(HTTPException) as ei:
        fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}")
    assert ei.value.status_code == 429


def test_heartbeat_contract_bounds_modules():
    from app.fleet.heartbeat import ModuleReport

    with pytest.raises(ValidationError):
        build_heartbeat(deployment_id="dep_a", reported_at="t",
                        modules=[ModuleReport(module_id=f"m{i}") for i in range(51)])


# --- fleet.v2 contract + dual-version ingest ----------------------------------

def _heartbeat_body_v2(deployment_id: str = "dep_a", *, reported_at: str = "",
                       update: UpdateReport | None = None) -> FleetHeartbeatV2:
    from datetime import datetime, timezone

    return build_heartbeat_v2(
        deployment_id=deployment_id,
        reported_at=reported_at or datetime.now(timezone.utc).isoformat(),
        version="2026.07.1", migration_revision="0019_trust_primitives",
        onebrain_healthy=True, chunks=12, users=3, accounts=1, uptime_seconds=90,
        update=update,
    )


def test_v2_heartbeat_schema_closed_and_healthy_rollup():
    from app.fleet.heartbeat import ModuleReport

    body = _heartbeat_body_v2()
    assert body.contract_version == CONTRACT_VERSION_V2
    assert body.healthy is True
    # extra="forbid" — an out-of-contract field (a smuggled name/email/text) is rejected.
    with pytest.raises(ValidationError):
        FleetHeartbeatV2(
            deployment_id="dep_a", reported_at="t", onebrain=body.onebrain,
            customer_email="leak@example.com",
        )
    unhealthy = build_heartbeat_v2(
        deployment_id="dep_a", reported_at="t", onebrain_healthy=True,
        modules=[ModuleReport(module_id="communication-api", healthy=False)])
    assert unhealthy.healthy is False  # one unhealthy module flips the rollup


def test_update_report_outcome_vocabulary():
    from app.fleet.heartbeat import UPDATE_OUTCOMES

    for outcome in UPDATE_OUTCOMES:
        assert UpdateReport(outcome=outcome).outcome == outcome
    with pytest.raises(ValidationError):
        UpdateReport(outcome="exploded")
    with pytest.raises(ValidationError):
        build_heartbeat_v2(deployment_id="dep_a", reported_at="t", uptime_seconds=-1)
    # Reserved backup evidence slots (C3): bounded vocabulary, inert default.
    for status in ("", "success", "failed"):
        assert UpdateReport(backup_status=status).backup_status == status
    with pytest.raises(ValidationError):
        UpdateReport(backup_status="corrupt")
    assert UpdateReport().backup_status == ""


def test_missing_discriminator_is_rejected():
    # A2 — accepted contract tightening: the discriminated union requires the
    # contract_version key to be PRESENT. A v1 body must CARRY it, not rely on
    # the model default; every real reporter emits it (model_dump includes
    # defaults). Pinned here so nobody discovers the 422 in production.
    from pydantic import TypeAdapter

    adapter = TypeAdapter(AnyFleetHeartbeat)
    v1_shaped = _heartbeat_body("dep_a").model_dump()
    del v1_shaped["contract_version"]
    with pytest.raises(ValidationError):
        adapter.validate_python(v1_shaped)

    validated = adapter.validate_python({**v1_shaped, "contract_version": "fleet.v1"})
    assert isinstance(validated, FleetHeartbeat)


def test_ingest_accepts_v1_and_v2(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    ack_v1 = fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}")
    assert ack_v1.received is True

    ack_v2 = fleet_router.ingest_heartbeat(_heartbeat_body_v2("dep_a"), authorization=f"Bearer {token}")
    assert ack_v2.received is True and ack_v2.deployment_id == "dep_a"

    latest = store.latest_heartbeat("dep_a")
    assert latest.contract_version == CONTRACT_VERSION_V2
    assert latest.payload["update"]["outcome"] == "none"


def test_ingest_v2_skew_guard_still_applies(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)

    stale = _heartbeat_body_v2("dep_a", reported_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(HTTPException) as ei:
        fleet_router.ingest_heartbeat(stale, authorization=f"Bearer {token}")
    assert ei.value.status_code == 400


# --- desired-state emission (P4-05) ------------------------------------------

from app.trust.envelope import DesiredStateEnvelope, VersionFloorState, verify_desired_state  # noqa: E402
from app.trust.release import parse_registry_allowlist, sign_release  # noqa: E402
from app.trust.signing import generate_keypair as _ds_generate_keypair  # noqa: E402

_DS_WRAP_PRIV, _DS_WRAP_PUB = _ds_generate_keypair()   # MC online wrapper key (D-11)
_DS_REL_PRIV, _DS_REL_PUB = _ds_generate_keypair()     # OFFLINE release key
_DS_ALLOW = parse_registry_allowlist("ghcr.io/proark1")
_DS_IMG = "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64


def _ds_settings(*, key: str = _DS_WRAP_PRIV, ttl: int = 900):
    # max_skew=0 disables the ingest skew guard so the ack tests can pin a fixed body.
    from types import SimpleNamespace
    return SimpleNamespace(fleet_desired_state_private_key=key, fleet_desired_state_ttl_seconds=ttl,
                           fleet_heartbeat_max_skew_seconds=0)


def _ds_signed_release(version: str = "2026.07.0") -> ReleaseManifest:
    from app.controlplane.base import ReleaseManifest as _RM
    fields = dict(version=version, git_sha="abc123", modules={"onebrain-api": "0.8.0"},
                  images={"onebrain-api": _DS_IMG}, migration_from="0041",
                  migration_to="0041", rollback_kind="")
    return _RM(status="published", signature=sign_release(fields, _DS_REL_PRIV), **fields)


def _ds_verify(envelope_dict: dict) -> list:
    from datetime import datetime, timezone
    env = DesiredStateEnvelope.model_validate(envelope_dict)
    return verify_desired_state(
        env, desired_state_public_key_b64=_DS_WRAP_PUB, release_public_key_b64=_DS_REL_PUB,
        expected_deployment_id="dep_a", now=datetime.now(timezone.utc),
        floor_state=VersionFloorState(), registry_allowlist=_DS_ALLOW)


def test_desired_state_endpoint_requires_matching_fleet_key(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")   # key pinned to dep_a
    control = _control_with("dep_a")
    control.create_release(_ds_signed_release())
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _ds_settings)

    # A box cannot fetch another deployment's desired-state.
    with pytest.raises(HTTPException) as ei:
        fleet_router.get_desired_state(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_b")
    assert ei.value.status_code == 403


def test_desired_state_endpoint_returns_wrapped_envelope(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    control = _control_with("dep_a")   # current_version 2026.07.0
    control.create_release(_ds_signed_release("2026.07.0"))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _ds_settings)

    out = fleet_router.get_desired_state(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert set(out) == {"envelope", "attempt_id"}
    assert out["attempt_id"] == ""            # steady-state confirm (no active rollout)
    assert _ds_verify(out["envelope"]) == []  # the served envelope verifies via app.trust


def test_desired_state_endpoint_none_when_emission_off(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    control = _control_with("dep_a")
    control.create_release(_ds_signed_release())
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", lambda: _ds_settings(key=""))

    assert fleet_router.get_desired_state(authorization=f"Bearer {token}",
                                          x_onebrain_deployment_id="dep_a") is None


def test_heartbeat_ack_is_inert_when_emission_off(monkeypatch):
    # With no wrapper key configured, the ack carries an EMPTY config — byte-for-byte
    # identical to today's fleet ack (the dormancy guarantee).
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    ack = fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}")
    assert ack.config == {}


def test_heartbeat_ack_carries_advisory_when_on(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    control = _control_with("dep_a")
    control.create_release(_ds_signed_release("2026.07.0"))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _ds_settings)

    ack = fleet_router.ingest_heartbeat(_heartbeat_body("dep_a"), authorization=f"Bearer {token}")
    advisory = ack.config["desired_state"]
    assert advisory["attempt_id"] == ""
    assert _ds_verify(advisory["envelope"]) == []   # the advisory envelope verifies too


# --- floor-bump serving (P5-01) ----------------------------------------------

from app.trust.envelope import FloorBump, sign_floor_bump  # noqa: E402

_FB_REL_PRIV, _FB_REL_PUB = _ds_generate_keypair()   # OFFLINE release key for bumps


def _fb_settings(*, verify_key: str = _FB_REL_PUB):
    from types import SimpleNamespace
    return SimpleNamespace(release_verify_public_key=verify_key)


def _signed_bump(floor_version: str = "2026.07.5", *, scope: str = "*",
                 priv: str = _FB_REL_PRIV) -> dict:
    bump = sign_floor_bump(
        FloorBump(deployment_scope=scope, floor_version=floor_version,
                  issued_at="2026-07-12T00:00:00+00:00"),
        priv)
    return bump.model_dump()


def test_floor_bump_serve_returns_null_when_none_set(monkeypatch):
    # Inertness: no served rows -> serve returns None (update.sh step 0 no-ops).
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    assert fleet_router.get_floor_bump(authorization=f"Bearer {token}",
                                       x_onebrain_deployment_id="dep_a") is None


def test_floor_bump_set_verify_serve_roundtrip(monkeypatch):
    store = MemoryFleetStore()
    token = _minted_key(store, "dep_a")
    control = _control_with("dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _fb_settings)

    info = fleet_router.set_floor_bump(
        fleet_router.FloorBumpSet(bump=_signed_bump("2026.07.5")), principal=_principal("admin"))
    assert info.scope == "*" and info.floor_version == "2026.07.5"

    served = fleet_router.get_floor_bump(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert served.floor_bump["floor_version"] == "2026.07.5"
    # The served bytes verify on the box (feed them to the app-free twin).
    bv = _load_box_verify_twin()
    assert bv.verify_floor_bump(served.floor_bump, release_public_key_b64=_FB_REL_PUB,
                                expected_deployment_id="dep_a") == []


def test_floor_bump_scoped_takes_precedence_over_fleet_wide(monkeypatch):
    store = MemoryFleetStore()
    token_a = _minted_key(store, "dep_a")
    token_b = _minted_key(store, "dep_b")
    control = MemoryControlPlaneStore()
    for dep in ("dep_a", "dep_b"):
        control.create_deployment(CustomerDeployment(id=dep, customer_name=dep, release_ring="pilot"))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _fb_settings)

    fleet_router.set_floor_bump(fleet_router.FloorBumpSet(bump=_signed_bump("2026.07.1", scope="*")),
                                principal=_principal("admin"))
    fleet_router.set_floor_bump(fleet_router.FloorBumpSet(bump=_signed_bump("2026.07.9", scope="dep_a")),
                                principal=_principal("admin"))

    # dep_a gets its scoped bump; dep_b falls back to the fleet-wide '*'.
    a = fleet_router.get_floor_bump(authorization=f"Bearer {token_a}", x_onebrain_deployment_id="dep_a")
    b = fleet_router.get_floor_bump(authorization=f"Bearer {token_b}", x_onebrain_deployment_id="dep_b")
    assert a.floor_bump["floor_version"] == "2026.07.9"
    assert b.floor_bump["floor_version"] == "2026.07.1"


def test_floor_bump_set_rejects_bad_signature(monkeypatch):
    store = MemoryFleetStore()
    control = _control_with("dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    # A verify key that does NOT match the throwaway signer -> signature_invalid.
    throwaway_priv, _ = _ds_generate_keypair()
    monkeypatch.setattr(fleet_router, "get_settings", _fb_settings)  # verify with _FB_REL_PUB
    with pytest.raises(HTTPException) as ei:
        fleet_router.set_floor_bump(
            fleet_router.FloorBumpSet(bump=_signed_bump("2026.07.5", priv=throwaway_priv)),
            principal=_principal("admin"))
    assert ei.value.status_code == 400


def test_floor_bump_set_409_when_verify_key_unset(monkeypatch):
    store = MemoryFleetStore()
    control = _control_with("dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", lambda: _fb_settings(verify_key=""))
    with pytest.raises(HTTPException) as ei:
        fleet_router.set_floor_bump(
            fleet_router.FloorBumpSet(bump=_signed_bump("2026.07.5")), principal=_principal("admin"))
    assert ei.value.status_code == 409


def test_floor_bump_clear_and_list(monkeypatch):
    store = MemoryFleetStore()
    control = _control_with("dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _fb_settings)

    fleet_router.set_floor_bump(fleet_router.FloorBumpSet(bump=_signed_bump("2026.07.5")),
                                principal=_principal("admin"))
    listed = fleet_router.list_floor_bumps(principal=_principal("admin"))
    assert [b.scope for b in listed] == ["*"]

    assert fleet_router.clear_floor_bump(scope="*", principal=_principal("admin")) == {"cleared": "*"}
    assert fleet_router.list_floor_bumps(principal=_principal("admin")) == []
    with pytest.raises(HTTPException) as ei:
        fleet_router.clear_floor_bump(scope="*", principal=_principal("admin"))  # already gone
    assert ei.value.status_code == 404


def test_floor_bump_set_clear_list_are_operator_admin_only(monkeypatch):
    store = MemoryFleetStore()
    control = _control_with("dep_a")
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_settings", _fb_settings)
    non_admin = _principal("front_desk")
    with pytest.raises(HTTPException) as ei:
        fleet_router.set_floor_bump(fleet_router.FloorBumpSet(bump=_signed_bump()), principal=non_admin)
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException):
        fleet_router.clear_floor_bump(scope="*", principal=non_admin)
    with pytest.raises(HTTPException):
        fleet_router.list_floor_bumps(principal=non_admin)


# --- wrapper-key rotation endpoints + G1-1 interlock + G1-3 convergence (P5-02) ---

def _rot_settings(*, priv="", pubs="", pub=""):
    from types import SimpleNamespace
    return SimpleNamespace(fleet_desired_state_private_key=priv,
                           fleet_desired_state_public_keys=pubs,
                           fleet_desired_state_public_key=pub)


def _prov_with_bundles(*deployment_ids):
    from app.provisioning.runs import BoxSecretBundle, MemoryProvisioningRunStore
    prov = MemoryProvisioningRunStore()
    for dep in deployment_ids:
        prov.upsert_secret_bundle(BoxSecretBundle(deployment_id=dep, account_id="", ciphertext="ct"))
    return prov


def test_rotate_desired_state_key_bumps_only_boxed_epochs(monkeypatch):
    control = MemoryControlPlaneStore()
    for dep in ("dep_a", "dep_b", "dep_c"):
        control.create_deployment(CustomerDeployment(id=dep, customer_name=dep, release_ring="pilot"))
    prov = _prov_with_bundles("dep_a", "dep_b")   # dep_c has no bundle (e.g. Railway) -> skipped
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(fleet_router, "get_settings", lambda: _rot_settings())  # emission off -> interlock skipped

    out = fleet_router.rotate_desired_state_key(principal=_principal("admin"))
    assert out.rotated == 2
    assert prov.get_secret_bundle("dep_a").secrets_epoch == 1
    assert prov.get_secret_bundle("dep_b").secrets_epoch == 1
    assert prov.get_secret_bundle("dep_c") is None


def test_rotate_endpoints_409_when_active_signer_excluded(monkeypatch):
    control = _control_with("dep_a")
    prov = _prov_with_bundles("dep_a")
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_provisioning_run_store", lambda: prov)
    # Signing with _DS_WRAP_PRIV but the served set EXCLUDES its public key -> the brick config.
    monkeypatch.setattr(fleet_router, "get_settings",
                        lambda: _rot_settings(priv=_DS_WRAP_PRIV, pubs="someone-else"))
    for call in (lambda: fleet_router.rotate_desired_state_key(principal=_principal("admin")),
                 lambda: fleet_router.rotate_deployment_secrets("dep_a", principal=_principal("admin"))):
        with pytest.raises(HTTPException) as ei:
            call()
        assert ei.value.status_code == 409
        assert ei.value.detail == "active_signer_not_in_public_key_set"
    assert prov.get_secret_bundle("dep_a").secrets_epoch == 0  # nothing bumped while refused

    # Adding the derived public key to the set unblocks the rotation.
    monkeypatch.setattr(fleet_router, "get_settings",
                        lambda: _rot_settings(priv=_DS_WRAP_PRIV, pubs=f"someone-else,{_DS_WRAP_PUB}"))
    assert fleet_router.rotate_desired_state_key(principal=_principal("admin")).rotated == 1
    assert prov.get_secret_bundle("dep_a").secrets_epoch == 1


def test_rotate_deployment_secrets_404s_then_bumps(monkeypatch):
    control = _control_with("dep_a")   # dep_a exists, no bundle yet
    prov = _prov_with_bundles()
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(fleet_router, "get_settings", lambda: _rot_settings())

    with pytest.raises(HTTPException) as ei:      # unknown deployment
        fleet_router.rotate_deployment_secrets("ghost", principal=_principal("admin"))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei2:     # known deployment, no bundle
        fleet_router.rotate_deployment_secrets("dep_a", principal=_principal("admin"))
    assert ei2.value.status_code == 404

    prov.upsert_secret_bundle(_prov_with_bundles("dep_a").get_secret_bundle("dep_a"))
    out = fleet_router.rotate_deployment_secrets("dep_a", principal=_principal("admin"))
    assert out.deployment_id == "dep_a" and out.secrets_epoch == 1


def test_backfill_runtime_db_credentials_reseals_legacy_bundle_and_bumps_epoch(monkeypatch):
    """New runtime passwords are minted only inside MC, never by a customer box."""
    import json

    from app.provisioning.runs import BoxSecretBundle, MemoryProvisioningRunStore, OneTimeSecretCipher

    settings = _boot_settings()
    cipher = OneTimeSecretCipher(settings)
    legacy = {
        "POSTGRES_PASSWORD": "owner-password",
        "REDIS_PASSWORD": "redis-password",
    }
    prov = MemoryProvisioningRunStore()
    prov.upsert_secret_bundle(BoxSecretBundle(
        deployment_id="dep_a", account_id="acct_a",
        ciphertext=cipher.seal_bundle(json.dumps(legacy)),
    ))
    control = _control_with("dep_a")
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: control)
    monkeypatch.setattr(fleet_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(fleet_router, "get_settings", lambda: settings)

    out = fleet_router.backfill_runtime_db_credentials("dep_a", principal=_principal("admin"))
    assert out.deployment_id == "dep_a"
    assert out.updated is True and out.secrets_epoch == 1
    stored = prov.get_secret_bundle("dep_a")
    assert stored is not None and stored.secrets_epoch == 1
    updated = json.loads(cipher.open_bundle(stored.ciphertext))
    for key in (
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_WORKER_PASSWORD",
        "POSTGRES_ASSISTANT_PASSWORD",
        "POSTGRES_COMMUNICATION_PASSWORD",
        "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",
    ):
        assert isinstance(updated[key], str) and len(updated[key]) >= 32

    # Retrying after a completed backfill never rotates or replaces either
    # password; it is safe for an operator to re-run after checking a heartbeat.
    retried = fleet_router.backfill_runtime_db_credentials("dep_a", principal=_principal("admin"))
    assert retried.updated is False and retried.secrets_epoch == 1
    reread = json.loads(cipher.open_bundle(prov.get_secret_bundle("dep_a").ciphertext))
    for key in (
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_WORKER_PASSWORD",
        "POSTGRES_ASSISTANT_PASSWORD",
        "POSTGRES_COMMUNICATION_PASSWORD",
        "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",
    ):
        assert reread[key] == updated[key]


def test_rotation_endpoints_are_operator_admin_only():
    non_admin = _principal("front_desk")
    with pytest.raises(HTTPException) as ei:
        fleet_router.rotate_desired_state_key(principal=non_admin)
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei2:
        fleet_router.rotate_deployment_secrets("dep_a", principal=non_admin)
    assert ei2.value.status_code == 403
    with pytest.raises(HTTPException) as ei3:
        fleet_router.backfill_runtime_db_credentials("dep_a", principal=non_admin)
    assert ei3.value.status_code == 403


def test_overview_surfaces_applied_secrets_epoch(monkeypatch):
    fleet = MemoryFleetStore()
    fleet.record_heartbeat(Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION_V2, "2026-07-12T00:00:00+00:00", "2026-07-12T00:00:01+00:00",
        True, version="2026.07.1",
        payload=_heartbeat_body_v2("dep_a", update=UpdateReport(applied_secrets_epoch=3)).model_dump()))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))

    out = fleet_router.fleet_overview(principal=_principal("admin"))
    assert out.deployments[0].applied_secrets_epoch == 3


def test_overview_applied_epoch_defaults_zero_for_v1_heartbeat(monkeypatch):
    # An old (v1) heartbeat carries no update block -> the epoch defaults to 0.
    fleet = MemoryFleetStore()
    fleet.record_heartbeat(Heartbeat(
        "hb", "dep_a", CONTRACT_VERSION, "2026-07-12T00:00:00+00:00", "2026-07-12T00:00:01+00:00",
        True, version="2026.07.0", payload=_heartbeat_body("dep_a").model_dump()))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    out = fleet_router.fleet_overview(principal=_principal("admin"))
    assert out.deployments[0].applied_secrets_epoch == 0


def test_update_report_applied_secrets_epoch_round_trips_and_defaults():
    # Additive field survives the v2 heartbeat round-trip...
    hb = _heartbeat_body_v2("dep_a", update=UpdateReport(applied_secrets_epoch=7))
    assert FleetHeartbeatV2.model_validate(hb.model_dump()).update.applied_secrets_epoch == 7
    # ...and an UpdateReport omitting it defaults to 0 (old-box compat; extra="forbid"
    # tolerates a MISSING field, only rejects UNKNOWN ones).
    assert UpdateReport().applied_secrets_epoch == 0
    assert UpdateReport.model_validate({"outcome": "succeeded"}).applied_secrets_epoch == 0


def test_update_report_backup_manifest_round_trips_and_defaults():
    # 7d/A17: the additive backup_manifest survives the v2 heartbeat round-trip...
    manifest = "sha256:" + "b" * 64 + ":2048"
    hb = _heartbeat_body_v2("dep_a", update=UpdateReport(backup_manifest=manifest))
    assert FleetHeartbeatV2.model_validate(hb.model_dump()).update.backup_manifest == manifest
    # ...and an UpdateReport omitting it defaults to "" (old-box compat; extra="forbid"
    # tolerates a MISSING field). Ships alongside applied_secrets_epoch (both additive).
    assert UpdateReport().backup_manifest == ""
    assert UpdateReport.model_validate({"outcome": "succeeded"}).backup_manifest == ""


def test_update_state_round_trips_backup_manifest(tmp_path):
    # 7d: update_state.py round-trips the whole model (no code change needed), so a
    # box-written manifest survives read-back into the reporter.
    import json

    from app.fleet.update_state import read_update_report, update_state_path

    manifest = "sha256:" + "c" * 64 + ":512"
    path = update_state_path(str(tmp_path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"outcome": "succeeded", "backup_status": "success",
                   "backup_ts": "2026-07-12T00:00:00+00:00", "backup_manifest": manifest}, fh)
    report = read_update_report(path)
    assert report.backup_manifest == manifest and report.backup_status == "success"


# --- bootstrap-token secret exchange (P5-03) ---------------------------------

_BOOT_KEY = "unit-test-secret-key"


def _boot_settings(*, priv="", pubs="", pub="", rate=5, window=60):
    from types import SimpleNamespace
    return SimpleNamespace(
        secret_encryption_key=_BOOT_KEY, secret_encryption_key_version="v1",
        bootstrap_secret_ttl_seconds=3600,
        fleet_desired_state_private_key=priv,
        fleet_desired_state_public_keys=pubs, fleet_desired_state_public_key=pub,
        fleet_bootstrap_rate_limit=rate, fleet_bootstrap_rate_window_seconds=window)


def _seed_bundle(prov, settings, *, dep="dep_a", epoch=0, pubs="pub-from-seal", ciphertext=None):
    import json
    from app.provisioning.runs import BoxSecretBundle, OneTimeSecretCipher
    if ciphertext is None:
        bundle = {"POSTGRES_PASSWORD": "pg", "POSTGRES_APP_PASSWORD": "app", "POSTGRES_WORKER_PASSWORD": "worker", "REDIS_PASSWORD": "rd", "ONEBRAIN_FLEET_KEY": "fk_x_y",
                  "ONEBRAIN_ADMIN_PASSWORD": "otp", "UPDATE_BACKUP_KEY": "bk",
                  "UPDATE_DESIRED_STATE_PUBLIC_KEYS": pubs}
        ciphertext = OneTimeSecretCipher(settings).seal_bundle(json.dumps(bundle))
    prov.upsert_secret_bundle(BoxSecretBundle(deployment_id=dep, account_id="acct",
                                              ciphertext=ciphertext, secrets_epoch=epoch))


def _seed_token(prov, *, dep="dep_a", ttl_seconds=3600):
    from datetime import datetime, timedelta, timezone
    from app.fleet.keys import generate_bootstrap_token, hash_secret
    from app.provisioning.runs import BoxBootstrapToken
    _, secret, raw = generate_bootstrap_token()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    prov.create_bootstrap_token(BoxBootstrapToken(token_hash=hash_secret(secret),
                                                  deployment_id=dep, expires_at=expires))
    return raw


def _wire_bootstrap(monkeypatch, prov, settings, *, fleet=None):
    from app.auth.throttle import RateLimiter
    from app.provisioning.runs import MemoryProvisioningRunStore  # noqa: F401 (type hint clarity)
    fleet = fleet if fleet is not None else MemoryFleetStore()
    monkeypatch.setattr(fleet_router, "get_provisioning_run_store", lambda: prov)
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: fleet)
    monkeypatch.setattr(fleet_router, "get_settings", lambda: settings)
    limiter = RateLimiter(settings.fleet_bootstrap_rate_limit, settings.fleet_bootstrap_rate_window_seconds)
    monkeypatch.setattr(fleet_router, "get_fleet_bootstrap_rate_limiter", lambda: limiter)
    return fleet


def _prov():
    from app.provisioning.runs import MemoryProvisioningRunStore
    return MemoryProvisioningRunStore()


def test_bootstrap_first_boot_exchange_returns_dotenv(monkeypatch):
    prov = _prov()
    settings = _boot_settings()
    _seed_bundle(prov, settings, epoch=0)
    token = _seed_token(prov)
    _wire_bootstrap(monkeypatch, prov, settings)

    out = fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert out.secrets_epoch == 0
    assert "POSTGRES_PASSWORD=pg" in out.dotenv and "ONEBRAIN_FLEET_KEY=fk_x_y" in out.dotenv
    # The token is consumed exactly once -> a replay 401s (forces the fleet-key path).
    with pytest.raises(HTTPException) as ei:
        fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert ei.value.status_code == 401


def test_bootstrap_overlays_current_pubkey_set_without_reencrypting(monkeypatch):
    # A wrapper-key rotation is reflected in the served dotenv via the settings overlay,
    # WITHOUT re-encrypting the stored bundle (which was sealed with a stale set).
    prov = _prov()
    settings = _boot_settings(pubs="new-pub-a,new-pub-b")
    _seed_bundle(prov, settings, pubs="STALE-SEALED-SET")
    token = _seed_token(prov)
    _wire_bootstrap(monkeypatch, prov, settings)

    out = fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert "UPDATE_DESIRED_STATE_PUBLIC_KEYS=new-pub-a,new-pub-b" in out.dotenv
    assert "STALE-SEALED-SET" not in out.dotenv


def test_bootstrap_fleet_key_rotation_refetch(monkeypatch):
    prov = _prov()
    settings = _boot_settings()
    _seed_bundle(prov, settings, epoch=3)
    fleet = MemoryFleetStore()
    key_token = _minted_key(fleet, "dep_a")
    _wire_bootstrap(monkeypatch, prov, settings, fleet=fleet)

    out = fleet_router.bootstrap_exchange(authorization=f"Bearer {key_token}", x_onebrain_deployment_id="dep_a")
    assert out.secrets_epoch == 3        # rotation re-fetch returns the current epoch
    # The fleet-key path consumes no token and is re-callable.
    assert fleet_router.bootstrap_exchange(authorization=f"Bearer {key_token}",
                                           x_onebrain_deployment_id="dep_a").secrets_epoch == 3


def test_bootstrap_404_when_no_bundle(monkeypatch):
    # Inertness: no bundle row -> 404 (dormant until a Hetzner box is provisioned).
    prov = _prov()
    settings = _boot_settings()
    fleet = MemoryFleetStore()
    key_token = _minted_key(fleet, "dep_a")
    _wire_bootstrap(monkeypatch, prov, settings, fleet=fleet)
    with pytest.raises(HTTPException) as ei:
        fleet_router.bootstrap_exchange(authorization=f"Bearer {key_token}", x_onebrain_deployment_id="dep_a")
    assert ei.value.status_code == 404


def test_bootstrap_rate_limit_is_dedicated_and_low(monkeypatch):
    # G1-5: a dedicated LOW budget (here 1/window) so a leaked key cannot poll the bundle.
    prov = _prov()
    settings = _boot_settings(rate=1)
    _seed_bundle(prov, settings)
    fleet = MemoryFleetStore()
    key_token = _minted_key(fleet, "dep_a")
    _wire_bootstrap(monkeypatch, prov, settings, fleet=fleet)

    assert fleet_router.bootstrap_exchange(authorization=f"Bearer {key_token}",
                                           x_onebrain_deployment_id="dep_a").secrets_epoch == 0
    with pytest.raises(HTTPException) as ei:      # second call within the window -> 429
        fleet_router.bootstrap_exchange(authorization=f"Bearer {key_token}", x_onebrain_deployment_id="dep_a")
    assert ei.value.status_code == 429


def test_bootstrap_409_when_active_signer_excluded(monkeypatch):
    # G1-1: never hand a box a pubkey set (overlaid from settings) that excludes MC's
    # active signer.
    prov = _prov()
    settings = _boot_settings(priv=_DS_WRAP_PRIV, pubs="someone-else")   # signer NOT in the set
    _seed_bundle(prov, settings)
    token = _seed_token(prov)
    _wire_bootstrap(monkeypatch, prov, settings)
    with pytest.raises(HTTPException) as ei:
        fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert ei.value.status_code == 409 and ei.value.detail == "active_signer_not_in_public_key_set"
    # The 409 fires BEFORE the token is consumed, so the box can retry once the config is fixed.
    assert prov.get_bootstrap_token(fleet_router.hash_secret(fleet_router.parse_bootstrap_token(token)[1])).consumed_at == ""


def test_bootstrap_lost_response_does_not_burn_token(monkeypatch):
    # G1-2: if bundle load raises (here a corrupt ciphertext), the token is NOT consumed;
    # a retry succeeds once the bundle is fixed.
    prov = _prov()
    settings = _boot_settings()
    _seed_bundle(prov, settings, ciphertext="corrupt-not-a-fernet-token")
    token = _seed_token(prov)
    from app.fleet.keys import hash_secret, parse_bootstrap_token
    token_hash = hash_secret(parse_bootstrap_token(token)[1])
    _wire_bootstrap(monkeypatch, prov, settings)

    with pytest.raises(HTTPException) as ei:
        fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert ei.value.status_code == 500
    assert prov.get_bootstrap_token(token_hash).consumed_at == ""    # token NOT burned

    # Fix the bundle -> the SAME token now succeeds exactly once.
    _seed_bundle(prov, settings)     # re-seal a valid bundle
    out = fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert "POSTGRES_PASSWORD=pg" in out.dotenv
    assert prov.get_bootstrap_token(token_hash).consumed_at != ""    # consumed now


def test_bootstrap_rejects_malformed_and_expired_token(monkeypatch):
    prov = _prov()
    settings = _boot_settings()
    _seed_bundle(prov, settings)
    expired = _seed_token(prov, ttl_seconds=-10)   # already expired
    _wire_bootstrap(monkeypatch, prov, settings)
    for tok in ("bt_only", expired):
        with pytest.raises(HTTPException) as ei:
            fleet_router.bootstrap_exchange(authorization=f"Bearer {tok}", x_onebrain_deployment_id="dep_a")
        assert ei.value.status_code == 401


def test_bootstrap_dotenv_never_logged(monkeypatch, caplog):
    import logging
    prov = _prov()
    settings = _boot_settings()
    _seed_bundle(prov, settings)
    token = _seed_token(prov)
    _wire_bootstrap(monkeypatch, prov, settings)
    with caplog.at_level(logging.DEBUG):
        out = fleet_router.bootstrap_exchange(authorization=f"Bearer {token}", x_onebrain_deployment_id="dep_a")
    assert "POSTGRES_PASSWORD=pg" in out.dotenv           # the secret IS delivered...
    assert "POSTGRES_PASSWORD=pg" not in caplog.text       # ...but never logged (G1-5)


def test_collect_reads_applied_secrets_epoch(tmp_path):
    # G1-3: the reporter emits the secrets_epoch the box last applied (a sibling of
    # update_state.json), so the operator can watch rotation converge.
    from app.config import Settings
    from app.fleet.reporter import collect_heartbeat
    (tmp_path / "secrets_epoch").write_text("4\n", encoding="utf-8")
    hb = collect_heartbeat(Settings(deployment_id="dep_local", data_dir=str(tmp_path)))
    assert hb.model_dump()["update"]["applied_secrets_epoch"] == 4
    # Absent (Railway / never exchanged) -> the inert default 0.
    hb2 = collect_heartbeat(Settings(deployment_id="dep_local", data_dir=str(tmp_path / "none")))
    assert hb2.model_dump()["update"]["applied_secrets_epoch"] == 0


def _load_box_verify_twin():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "deploy" / "box" / "onebrain_box_verify.py"
    spec = importlib.util.spec_from_file_location("onebrain_box_verify", path)
    mod = importlib.util.module_from_spec(spec)
    import sys as _sys
    _sys.modules["onebrain_box_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_v2_payload_round_trips_update_report():
    hb = _heartbeat_body_v2(
        "dep_a", update=UpdateReport(last_target_version="2026.07.2", outcome="succeeded",
                                     migration_reached="0019_trust_primitives", attempt_id="ro_1",
                                     ts="2026-07-12T00:00:00+00:00"))
    assert FleetHeartbeatV2.model_validate(hb.model_dump()) == hb


# --- enrollment + analytics (Phase 3) ----------------------------------------

def test_fleet_enrollment_vars_pure():
    from app.fleet.enrollment import fleet_enrollment_vars
    env = fleet_enrollment_vars("https://mc.example/", "dep_a", "fk_id_secret")
    assert env == {"ONEBRAIN_FLEET_URL": "https://mc.example",
                   "ONEBRAIN_DEPLOYMENT_ID": "dep_a", "ONEBRAIN_FLEET_KEY": "fk_id_secret"}


def test_mint_deployment_fleet_key_stores_hash_only():
    from app.fleet.enrollment import mint_deployment_fleet_key
    store = MemoryFleetStore()
    key_id, token = mint_deployment_fleet_key(store, "dep_a", label="enroll", now_iso="t")
    assert token.startswith("fk_")
    stored = store.get_key(key_id)
    assert stored is not None and stored.deployment_id == "dep_a"
    assert stored.key_hash != token  # only the hash is persisted


def test_enroll_endpoint_mints_and_returns_env(monkeypatch):
    from types import SimpleNamespace
    store = MemoryFleetStore()
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    monkeypatch.setattr(fleet_router, "get_settings",
                        lambda: SimpleNamespace(fleet_public_url="https://mc.example"))

    out = fleet_router.enroll_deployment("dep_a", principal=_principal("admin"))
    assert out.deployment_id == "dep_a"
    assert out.env["ONEBRAIN_DEPLOYMENT_ID"] == "dep_a"
    assert out.env["ONEBRAIN_FLEET_KEY"].startswith("fk_")
    assert out.env["ONEBRAIN_FLEET_URL"] == "https://mc.example"
    assert store.get_key(out.key_id) is not None  # key persisted (hash only)


def test_enroll_endpoint_guards(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: MemoryFleetStore())
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    # unknown deployment -> 404
    monkeypatch.setattr(fleet_router, "get_settings", lambda: SimpleNamespace(fleet_public_url="https://mc"))
    with pytest.raises(HTTPException) as ei:
        fleet_router.enroll_deployment("ghost", principal=_principal("admin"))
    assert ei.value.status_code == 404
    # no public url configured -> 409
    monkeypatch.setattr(fleet_router, "get_settings", lambda: SimpleNamespace(fleet_public_url=""))
    with pytest.raises(HTTPException) as ei:
        fleet_router.enroll_deployment("dep_a", principal=_principal("admin"))
    assert ei.value.status_code == 409
    # non-admin -> 403
    with pytest.raises(HTTPException) as ei:
        fleet_router.enroll_deployment("dep_a", principal=_principal("front_desk"))
    assert ei.value.status_code == 403


def test_memory_store_heartbeat_history_and_prune():
    store = MemoryFleetStore()
    for i, ts in enumerate(["2026-07-11T00:00:00+00:00", "2026-07-11T01:00:00+00:00", "2026-07-11T02:00:00+00:00"]):
        store.record_heartbeat(Heartbeat(f"hb{i}", "dep_a", CONTRACT_VERSION, ts, ts, True))
    history = store.list_heartbeats("dep_a")
    assert [h.id for h in history] == ["hb2", "hb1", "hb0"]  # newest first
    assert len(store.list_heartbeats("dep_a", since_iso="2026-07-11T01:00:00+00:00")) == 2
    assert len(store.list_heartbeats("dep_a", limit=1)) == 1

    removed = store.prune_heartbeats("2026-07-11T01:00:00+00:00")
    assert removed == 1  # hb0 dropped
    assert [h.id for h in store.list_heartbeats("dep_a")] == ["hb2", "hb1"]


def test_history_endpoint_returns_counts(monkeypatch):
    store = MemoryFleetStore()
    store.record_heartbeat(Heartbeat("hb", "dep_a", CONTRACT_VERSION, "t", "2026-07-11T00:00:00+00:00", True,
                                     payload=_heartbeat_body("dep_a").model_dump()))
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    out = fleet_router.heartbeat_history("dep_a", principal=_principal("admin"))
    assert out.total == 1
    assert out.points[0].counts["chunks"] == 12 and out.points[0].counts["users"] == 3


def test_enroll_rotates_prior_keys(monkeypatch):
    from types import SimpleNamespace
    store = MemoryFleetStore()
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: store)
    monkeypatch.setattr(fleet_router, "get_control_plane_store", lambda: _control_with("dep_a"))
    monkeypatch.setattr(fleet_router, "get_settings", lambda: SimpleNamespace(fleet_public_url="https://mc"))

    first = fleet_router.enroll_deployment("dep_a", principal=_principal("admin"))
    second = fleet_router.enroll_deployment("dep_a", principal=_principal("admin"))

    active = [k for k in store.list_keys("dep_a") if k.status == "active"]
    assert len(active) == 1 and active[0].id == second.key_id  # only the newest key is active
    assert store.get_key(first.key_id).status == "revoked"      # prior key rotated out


def test_prune_once_uses_retention_window():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.fleet.retention import prune_once
    store = MemoryFleetStore()
    store.record_heartbeat(Heartbeat("old", "dep_a", CONTRACT_VERSION, "t", "2000-01-01T00:00:00+00:00", True))
    store.record_heartbeat(Heartbeat("new", "dep_a", CONTRACT_VERSION, "t", datetime.now(timezone.utc).isoformat(), True))

    removed = prune_once(SimpleNamespace(fleet_heartbeat_retention_days=30), store)
    assert removed == 1  # only the year-2000 heartbeat is outside the window
    assert [h.id for h in store.list_heartbeats("dep_a")] == ["new"]

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
from app.fleet.heartbeat import CONTRACT_VERSION, FleetHeartbeat, build_heartbeat
from app.fleet.keys import generate_fleet_key, hash_secret, parse_fleet_key, verify_secret
from app.fleet.memory import MemoryFleetStore
from app.fleet.reporter import report_once, send_heartbeat
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
        id=deployment_id, customer_name="Customer A", deployment_type="dedicated_railway",
        release_ring="pilot", current_version="2026.07.0",
    ))
    return store


def _heartbeat_body(deployment_id: str = "dep_a", *, healthy: bool = True, version: str = "2026.07.0") -> FleetHeartbeat:
    return build_heartbeat(
        deployment_id=deployment_id, reported_at="2026-07-11T00:00:00+00:00",
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


# --- reporter ----------------------------------------------------------------

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

    assert hb.contract_version == CONTRACT_VERSION
    assert hb.deployment_id == "dep_local"
    assert hb.onebrain.migration_revision  # stamped from REQUIRED_ALEMBIC_REVISION
    # Everything in the payload is a count/flag/version — no free-text customer content.
    payload = hb.model_dump()
    assert set(payload) == {"contract_version", "deployment_id", "reported_at", "onebrain", "modules"}

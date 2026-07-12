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
    FleetHeartbeatV2, UpdateReport, build_heartbeat, build_heartbeat_v2,
)
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

    assert hb.contract_version == CONTRACT_VERSION_V2
    assert hb.deployment_id == "dep_local"
    assert hb.onebrain.version == "0.1.0"  # build_version unset -> app.__version__
    # Memory mode: no schema to attest — claim nothing; computed health holds.
    assert hb.onebrain.migration_revision == ""
    assert hb.onebrain.healthy is True
    # Everything in the payload is a count/flag/version/enum — no free-text customer content.
    payload = hb.model_dump()
    assert set(payload) == {"contract_version", "deployment_id", "reported_at", "onebrain", "modules", "update"}
    assert payload["update"]["outcome"] == "none"


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
    assert set(body) == {"contract_version", "deployment_id", "reported_at", "onebrain", "modules", "update"}


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

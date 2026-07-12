"""P5-06 (G3-2): the operator first-boot self-seed. Empty stores + operator_mode ->
exactly one `mc` deployment row + one active fleet key whose stored hash matches the
SECRET part of the baked ONEBRAIN_FLEET_KEY (what _authenticate_fleet_key verifies);
idempotent; a no-op off operator_mode; and a heartbeat POST with the raw key is then
accepted for `mc` end-to-end (self-heartbeat works, no Hetzner).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.self_seed import seed_operator_self_deployment
from app.fleet.keys import generate_fleet_key, parse_fleet_key, verify_secret
from app.fleet.memory import MemoryFleetStore


def _settings(token, **kw):
    base = dict(operator_mode=True, deployment_id="mc", fleet_key=token, build_version="2026.07.1")
    base.update(kw)
    return SimpleNamespace(**base)


def test_self_seed_creates_row_and_matching_key():
    _, _, token = generate_fleet_key()
    control, fleet = MemoryControlPlaneStore(), MemoryFleetStore()

    assert seed_operator_self_deployment(_settings(token), control, fleet) is True

    dep = control.get_deployment("mc")
    assert dep is not None and dep.deployment_type == "dedicated_server" and dep.release_ring == "manual"
    keys = fleet.list_keys("mc")
    assert len(keys) == 1 and keys[0].status == "active"
    # The stored hash matches the SECRET part of the baked token — the exact value
    # _authenticate_fleet_key verifies (parse -> get_key(id) -> verify_secret(secret)).
    key_id, secret = parse_fleet_key(token)
    assert keys[0].id == key_id and verify_secret(secret, keys[0].key_hash)


def test_self_seed_is_idempotent():
    _, _, token = generate_fleet_key()
    control, fleet = MemoryControlPlaneStore(), MemoryFleetStore()

    assert seed_operator_self_deployment(_settings(token), control, fleet) is True
    assert seed_operator_self_deployment(_settings(token), control, fleet) is False  # row present -> no-op
    assert len(fleet.list_keys("mc")) == 1  # no duplicate key


def test_self_seed_noop_off_operator_mode():
    _, _, token = generate_fleet_key()
    control, fleet = MemoryControlPlaneStore(), MemoryFleetStore()

    assert seed_operator_self_deployment(_settings(token, operator_mode=False), control, fleet) is False
    assert control.get_deployment("mc") is None and fleet.list_keys("mc") == []


def test_self_seeded_key_accepts_self_heartbeat(monkeypatch):
    """End-to-end (fakes, no Hetzner): after the self-seed, the reporter's self-heartbeat
    authenticating with the raw baked ONEBRAIN_FLEET_KEY is accepted for `mc`."""
    import app.routers.fleet as fleet_router
    from tests.test_fleet import _heartbeat_body

    _, _, token = generate_fleet_key()
    control, fleet = MemoryControlPlaneStore(), MemoryFleetStore()
    seed_operator_self_deployment(_settings(token), control, fleet)
    monkeypatch.setattr(fleet_router, "get_fleet_store", lambda: fleet)

    ack = fleet_router.ingest_heartbeat(_heartbeat_body("mc"), authorization=f"Bearer {token}")
    assert ack.received is True and ack.deployment_id == "mc"

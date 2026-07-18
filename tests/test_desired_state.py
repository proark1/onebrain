"""P4-05: MC desired-state emission — the pure compute/sign path
(app/controlplane/desired_state.py). Store + clock + keys injected; no network.

Includes the end-to-end round-trip that a Mission-Control-emitted, wrapper-signed
envelope passes the app-FREE box verifier (deploy/box/onebrain_box_verify.py): the
two independent implementations of the two-key chain agree on accept.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.controlplane.base import CustomerDeployment, DeploymentModule, ReleaseManifest, RolloutRun
from app.controlplane.desired_state import (
    active_pull_attempt_id,
    active_signer_in_served_set,
    active_wrapper_public_key,
    build_desired_state,
    served_public_key_set,
    sign_desired_state_for,
    target_release_for_deployment,
)
from app.controlplane.memory import MemoryControlPlaneStore
from app.trust.envelope import VersionFloorState, verify_desired_state
from app.trust.release import parse_registry_allowlist, sign_release
from app.trust.signing import generate_keypair

WRAPPER_PRIV, WRAPPER_PUB = generate_keypair()   # MC online wrapper key (D-11)
REL_PRIV, REL_PUB = generate_keypair()           # OFFLINE release key
ALLOW = parse_registry_allowlist("ghcr.io/proark1")
_IMG = "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64
NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)

_BOX_DIR = Path(__file__).resolve().parents[1] / "deploy" / "box"


def _load_box_verify():
    spec = importlib.util.spec_from_file_location("onebrain_box_verify", _BOX_DIR / "onebrain_box_verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["onebrain_box_verify"] = mod  # so @dataclass can resolve the module (importlib quirk)
    spec.loader.exec_module(mod)
    return mod


def _signed_release(version: str = "2026.07.1", *, migration_to: str = "0041") -> ReleaseManifest:
    fields = dict(version=version, git_sha="abc123", modules={"onebrain-api": "0.8.0"},
                  images={"onebrain-api": _IMG}, migration_from="0041",
                  migration_to=migration_to, rollback_kind="")
    return ReleaseManifest(status="published", signature=sign_release(fields, REL_PRIV), **fields)


def _settings(*, key: str = WRAPPER_PRIV, ttl: int = 900):
    return SimpleNamespace(fleet_desired_state_private_key=key, fleet_desired_state_ttl_seconds=ttl)


def _store(current_version: str = "2026.07.1") -> MemoryControlPlaneStore:
    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id="dep_a", customer_name="A", account_id="acct", release_ring="pilot",
        current_version=current_version, current_migration="0041"))
    store.upsert_module(DeploymentModule("dep_a", "onebrain-api", "0.7.0"))
    return store


def test_build_desired_state_embeds_stored_release_signature():
    release = _signed_release()
    env = build_desired_state(SimpleNamespace(id="dep_a"), release, floor_version=release.version,
                              now=NOW, ttl_seconds=900, nonce="abcd1234efgh")
    # The embedded block is a byte-for-byte copy of the offline-signed release fields.
    assert env.release.signature == release.signature
    assert env.release.version == release.version
    assert env.release.images == {"onebrain-api": _IMG}
    assert env.version_floor == release.version   # raise-only floor on the box
    assert env.deployment_id == "dep_a"
    assert env.envelope_signature == ""           # wrapper unsigned until sign_desired_state


def test_sign_and_verify_roundtrip():
    store = _store()
    store.create_release(_signed_release())
    env = sign_desired_state_for(store, "dep_a", settings=_settings(), now=NOW)
    assert env is not None and env.envelope_signature
    errors = verify_desired_state(
        env, desired_state_public_key_b64=WRAPPER_PUB, release_public_key_b64=REL_PUB,
        expected_deployment_id="dep_a", now=NOW, floor_state=VersionFloorState(),
        registry_allowlist=ALLOW)
    assert errors == []


def test_mc_envelope_passes_box_verifier():
    """The MC-emitted, wrapper-signed envelope verifies under the app-FREE box verifier
    (deploy/box/onebrain_box_verify.py) — MC and the box agree end-to-end on accept."""
    store = _store()
    store.create_release(_signed_release())
    env = sign_desired_state_for(store, "dep_a", settings=_settings(), now=NOW)
    bv = _load_box_verify()
    errors = bv.verify_desired_state(
        env.model_dump(), desired_state_public_key_b64=WRAPPER_PUB, release_public_key_b64=REL_PUB,
        expected_deployment_id="dep_a", now=NOW, floor_state=bv.FloorState(), registry_allowlist=ALLOW)
    assert errors == []
    # And the box derives the exact target update.sh would pull from the verified block.
    target = bv.verified_target(env.model_dump())
    assert target["modules"] == {"onebrain-api": "0.8.0"}
    assert target["images"] == {"onebrain-api": _IMG}


def test_emission_disabled_returns_none():
    store = _store()
    store.create_release(_signed_release())
    assert sign_desired_state_for(store, "dep_a", settings=_settings(key=""), now=NOW) is None


def test_unknown_deployment_returns_none():
    store = _store()
    store.create_release(_signed_release())
    assert sign_desired_state_for(store, "dep_nope", settings=_settings(), now=NOW) is None


def test_unsigned_release_is_never_offered():
    store = _store()
    unsigned = ReleaseManifest(version="2026.07.1", git_sha="abc123", modules={"onebrain-api": "0.8.0"},
                               images={"onebrain-api": _IMG}, migration_from="0041", migration_to="0041",
                               status="published")   # signature="" (default)
    store.create_release(unsigned)
    assert sign_desired_state_for(store, "dep_a", settings=_settings(), now=NOW) is None
    with pytest.raises(ValueError, match="unsigned release"):
        build_desired_state(SimpleNamespace(id="dep_a"), unsigned, floor_version="2026.07.1",
                            now=NOW, ttl_seconds=900, nonce="abcd1234efgh")


def test_active_rollout_target_wins_over_current():
    store = _store(current_version="2026.07.0")
    store.create_release(_signed_release(version="2026.07.0"))
    store.create_release(_signed_release(version="2026.07.2"))
    # No active rollout -> steady-state confirm targets the CURRENT version, no attempt.
    assert target_release_for_deployment(store, store.get_deployment("dep_a")).version == "2026.07.0"
    assert active_pull_attempt_id(store, "dep_a") == ""
    # An active (non-terminal) rollout to a newer version wins, and its id is the attempt.
    store.start_rollout(RolloutRun(id="roll_x", deployment_id="dep_a", target_version="2026.07.2",
                                   status="pending", started_by="op"))
    env = sign_desired_state_for(store, "dep_a", settings=_settings(), now=NOW)
    assert env.release.version == "2026.07.2"
    assert active_pull_attempt_id(store, "dep_a") == "roll_x"


# --- P5-02: G1-1 rotation interlock helpers -----------------------------------

def _interlock_settings(*, priv="", pubs="", pub=""):
    return SimpleNamespace(fleet_desired_state_private_key=priv,
                           fleet_desired_state_public_keys=pubs,
                           fleet_desired_state_public_key=pub)


def test_active_wrapper_public_key_derives_and_is_empty_when_unset():
    assert active_wrapper_public_key(_interlock_settings(priv=WRAPPER_PRIV)) == WRAPPER_PUB
    assert active_wrapper_public_key(_interlock_settings(priv="")) == ""


def test_served_public_key_set_prefers_csv_then_singular():
    assert served_public_key_set(_interlock_settings(pubs=" a , b ,, c ")) == ["a", "b", "c"]
    assert served_public_key_set(_interlock_settings(pub="only")) == ["only"]
    assert served_public_key_set(_interlock_settings()) == []


def test_active_signer_in_served_set_true_false_and_skip():
    # Present in the csv set -> True.
    assert active_signer_in_served_set(
        _interlock_settings(priv=WRAPPER_PRIV, pubs=f"other,{WRAPPER_PUB}")) is True
    # Present via the singular fallback -> True.
    assert active_signer_in_served_set(
        _interlock_settings(priv=WRAPPER_PRIV, pub=WRAPPER_PUB)) is True
    # Signing with a key ABSENT from the served set -> False (the brick config).
    assert active_signer_in_served_set(
        _interlock_settings(priv=WRAPPER_PRIV, pubs="someone-else")) is False
    # Emission disabled (no private key) -> True (nothing to sign, nothing to brick).
    assert active_signer_in_served_set(_interlock_settings(priv="", pubs="")) is True

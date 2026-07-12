"""P4-08: the app-free box verifier (deploy/box/onebrain_box_verify.py). Pure unit
tests + the anti-drift CONFORMANCE pin against app.trust.envelope (A18: enumerated
canonical-JSON/key/signature edge cases + a property test over random envelopes) +
floor advance / floor-bump tests. The box VERIFIES, never trusts MC (D2), so this
is the last line of defence and must never diverge from app.trust."""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.trust import envelope as E
from app.trust.envelope import FloorBump, sign_floor_bump
from app.trust.release import canonical_release_payload, parse_registry_allowlist
from app.trust.signing import generate_keypair, sign_payload

_BOX_DIR = Path(__file__).resolve().parents[1] / "deploy" / "box"


def _load_box_verify():
    spec = importlib.util.spec_from_file_location("onebrain_box_verify", _BOX_DIR / "onebrain_box_verify.py")
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules so @dataclass can resolve the module (importlib quirk).
    sys.modules["onebrain_box_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


bv = _load_box_verify()

REL_PRIV, REL_PUB = generate_keypair()
DS_PRIV, DS_PUB = generate_keypair()
ALLOW = parse_registry_allowlist("ghcr.io/proark1")
NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
_GOOD_IMG = "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64


def make_block(**over) -> E.SignedReleaseBlock:
    fields = dict(
        version="2026.7.2", git_sha="abc123",
        modules={"onebrain-api": "2026.7.2"}, images={"onebrain-api": _GOOD_IMG},
        migration_from="0019", migration_to="0020", rollback_kind="code_only",
    )
    fields.update(over)
    sig = sign_payload(canonical_release_payload(**fields), REL_PRIV)
    return E.SignedReleaseBlock(signature=sig, **fields)


def make_env(*, block=None, sign=True, **over) -> E.DesiredStateEnvelope:
    block = block if block is not None else make_block()
    fields = dict(
        deployment_id="dep_a", release=block, version_floor="",
        nonce="abcd1234efgh", issued_at="2026-07-12T00:00:00+00:00",
        expires_at="2026-07-12T23:59:00+00:00",
    )
    fields.update(over)
    env = E.DesiredStateEnvelope(**fields)
    return E.sign_desired_state(env, DS_PRIV) if sign else env


def app_verify(env, fs=None, *, expected=None):
    return E.verify_desired_state(
        env, desired_state_public_key_b64=DS_PUB, release_public_key_b64=REL_PUB,
        expected_deployment_id=expected or env.deployment_id, now=NOW,
        floor_state=fs or E.VersionFloorState(), registry_allowlist=ALLOW,
    )


def box_verify(env=None, fs=None, *, expected="dep_a", raw=None):
    raw = raw if raw is not None else json.dumps(env.model_dump()).encode()
    return bv.verify_bytes(
        raw, desired_state_public_key_b64=DS_PUB, release_public_key_b64=REL_PUB,
        expected_deployment_id=expected, now=NOW,
        floor_state=fs or bv.FloorState(), registry_allowlist=ALLOW,
    )


# --- direct unit tests -------------------------------------------------------
def test_verify_accepts_valid_envelope():
    assert box_verify(make_env()) == []


def test_rejects_release_not_offline_signed():
    # B1: wrapper valid, embedded release signature tampered -> release_signature_invalid.
    block = make_block()
    bad_block = block.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
    env = make_env(block=bad_block)   # re-signs the wrapper over the tampered block
    assert box_verify(env) == ["release_signature_invalid"]


def test_rejects_expired():
    env = make_env(expires_at="2026-07-12T00:00:00+00:00")   # before NOW
    assert box_verify(env) == ["expired"]


def test_rejects_reused_nonce():
    env = make_env()
    assert box_verify(env, bv.FloorState(seen_nonces=("abcd1234efgh",))) == ["nonce_reused"]


def test_rejects_off_allowlist_image():
    evil = "ghcr.io/evilorg/onebrain-api@sha256:" + "b" * 64
    env = make_env(block=make_block(images={"onebrain-api": evil}))
    result = box_verify(env)
    assert result and result[0].startswith("images_invalid:")
    assert "registry allowlist" in result[0]


def test_rejects_below_floor():
    env = make_env(block=make_block(version="2026.7.2"))
    assert box_verify(env, bv.FloorState(floor_version="2026.7.9")) == ["version_below_floor"]


# --- A18 conformance: identical accept/reject + first error code -------------
def _conformance_cases():
    valid_block = make_block()
    cases = []
    cases.append(("valid", make_env(), E.VersionFloorState(), "dep_a"))
    cases.append(("bad_wrapper_sig",
                  make_env().model_copy(update={"envelope_signature": base64.b64encode(b"z" * 64).decode()}),
                  E.VersionFloorState(), "dep_a"))
    cases.append(("bad_release_sig",
                  make_env(block=valid_block.model_copy(update={"signature": base64.b64encode(b"z" * 64).decode()})),
                  E.VersionFloorState(), "dep_a"))
    cases.append(("wrong_deployment", make_env(deployment_id="dep_zzz"), E.VersionFloorState(), "dep_a"))
    cases.append(("expired", make_env(expires_at="2000-01-01T00:00:00+00:00"), E.VersionFloorState(), "dep_a"))
    cases.append(("malformed_expiry", make_env(expires_at="not-a-timestamp"), E.VersionFloorState(), "dep_a"))
    cases.append(("reused_nonce", make_env(), E.VersionFloorState(seen_nonces=("abcd1234efgh",)), "dep_a"))
    cases.append(("below_floor", make_env(), E.VersionFloorState(floor_version="2027.1.0"), "dep_a"))
    cases.append(("floor_not_comparable", make_env(), E.VersionFloorState(floor_version="not.a.version"), "dep_a"))
    cases.append(("off_allowlist",
                  make_env(block=make_block(images={"onebrain-api": "ghcr.io/evilorg/onebrain-api@sha256:" + "c" * 64})),
                  E.VersionFloorState(), "dep_a"))
    cases.append(("unknown_module",
                  make_env(block=make_block(images={"not-a-module": _GOOD_IMG})),
                  E.VersionFloorState(), "dep_a"))
    cases.append(("empty_images", make_env(block=make_block(images={})), E.VersionFloorState(), "dep_a"))
    # A18 canonical-JSON / key / signature edge cases (must canonicalize identically):
    cases.append(("unicode_wrapper_field", make_env(deployment_id="dép_ünïcode"), E.VersionFloorState(), "dép_ünïcode"))
    cases.append(("unicode_release_field",
                  make_env(block=make_block(git_sha="ünïcode-sha", modules={"onebrain-api": "2026.7.2-café"})),
                  E.VersionFloorState(), "dep_a"))
    cases.append(("empty_string_module_key",
                  make_env(block=make_block(images={"": _GOOD_IMG})), E.VersionFloorState(), "dep_a"))
    cases.append(("oversized_git_sha", make_env(block=make_block(git_sha="a" * 64)), E.VersionFloorState(), "dep_a"))
    cases.append(("short_b64_sig",
                  make_env().model_copy(update={"envelope_signature": "AA"}), E.VersionFloorState(), "dep_a"))
    cases.append(("padded_b64_sig",
                  make_env().model_copy(update={"envelope_signature": "AAAA===="}), E.VersionFloorState(), "dep_a"))
    cases.append(("non_b64_sig",
                  make_env().model_copy(update={"envelope_signature": "!!!not-base64!!!"}), E.VersionFloorState(), "dep_a"))
    return cases


@pytest.mark.parametrize("label,env,floor,expected", _conformance_cases())
def test_box_verifier_conformance_with_app_trust(label, env, floor, expected):
    app_result = app_verify(env, floor, expected=expected)
    box_result = box_verify(env, bv.FloorState(floor_version=floor.floor_version, seen_nonces=floor.seen_nonces),
                            expected=expected)
    assert app_result == box_result, f"{label}: app={app_result} box={box_result}"


def test_canonical_json_matches_app_over_edge_cases():
    # Directly pin the canonical-JSON reimplementation (empty keys, unicode, key
    # order, int vs float, nesting) — the thing a naive reimplementation gets wrong.
    for case in (
        {"b": 1, "a": 2, "c": 3},
        {"": "empty-key", "z": ""},
        {"u": "café ünïcodé 日本語"},
        {"i": 1, "f": 1.0, "big": 10_000_000_000},
        {"nested": {"z": [3, 2, 1], "a": {"": 0}}},
        {"contract": "onebrain-release.v1", "version": "2026.7.2"},
    ):
        assert bv._canonical_json(case) == E._canonical_json(case), case


def test_duplicate_json_keys_last_wins_like_stdlib():
    # A18 "duplicate/again-inserted keys": json.loads keeps the LAST — the box
    # verifies the same as the deduplicated envelope.
    valid_raw = json.dumps(make_env().model_dump())
    dup_raw = "{" + '"nonce":"ZZZZZZZZ",' + valid_raw[1:]   # inject an earlier duplicate nonce
    assert box_verify(raw=valid_raw.encode()) == []
    assert box_verify(raw=dup_raw.encode()) == []


def test_malformed_json_is_rejected_not_crashed():
    assert box_verify(raw=b"{not json") == ["malformed_envelope"]
    assert box_verify(raw=b"[]") == ["malformed_envelope"]


def test_box_verifier_conformance_property():
    rng = random.Random(20260712)
    hexd = "0123456789abcdef"
    for _ in range(150):
        version = f"{rng.randint(2024, 2027)}.{rng.randint(1, 12)}.{rng.randint(0, 30)}"
        dep = "dep_" + "".join(rng.choice(hexd) for _ in range(6))
        nonce = "".join(rng.choice(hexd) for _ in range(rng.randint(8, 24)))
        org = rng.choice(["proark1", "proark1", "evilorg"])
        img = f"ghcr.io/{org}/onebrain-api@sha256:" + "".join(rng.choice(hexd) for _ in range(64))
        expired = rng.random() < 0.25
        exp = "2000-01-01T00:00:00+00:00" if expired else "2035-01-01T00:00:00+00:00"
        block = make_block(version=version, modules={"onebrain-api": version}, images={"onebrain-api": img})
        env = make_env(block=block, deployment_id=dep, nonce=nonce, expires_at=exp)

        corrupt = rng.choice([None, None, "wrapper", "release", "expiry"])
        if corrupt == "wrapper":
            env = env.model_copy(update={
                "envelope_signature": base64.b64encode(bytes(rng.randrange(256) for _ in range(64))).decode()})
        elif corrupt == "release":
            bad = block.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
            env = make_env(block=bad, deployment_id=dep, nonce=nonce, expires_at=exp)
        elif corrupt == "expiry":
            env = make_env(block=block, deployment_id=dep, nonce=nonce, expires_at=f"garbage-{rng.randint(0, 9)}")

        floor_version = rng.choice(["", "2025.1.0", "2027.12.30", version, "not.comparable"])
        seen = ("seen-nonce",) if rng.random() < 0.15 else ()
        if seen and rng.random() < 0.5:
            seen = (nonce,)   # sometimes make the nonce actually reused
        app_result = app_verify(env, E.VersionFloorState(floor_version=floor_version, seen_nonces=seen), expected=dep)
        box_result = box_verify(env, bv.FloorState(floor_version=floor_version, seen_nonces=seen), expected=dep)
        assert app_result == box_result, (env.model_dump(), floor_version, seen, app_result, box_result)


# --- floor advance / floor bump ----------------------------------------------
def test_floor_advance_is_raise_only():
    high = bv.FloorState(floor_version="2026.7.5")
    low_env = make_env(block=make_block(version="2026.7.2"))
    # a lower target never lowers the floor
    assert bv.advance_floor(high, low_env.model_dump()).floor_version == "2026.7.5"
    # a higher target raises it, and the nonce is recorded
    hi_env = make_env(block=make_block(version="2026.7.9"), nonce="noncehigh1234")
    advanced = bv.advance_floor(high, hi_env.model_dump())
    assert advanced.floor_version == "2026.7.9"
    assert "noncehigh1234" in advanced.seen_nonces
    # conformance with app.trust.advance_floor
    app_floor = E.advance_floor(E.VersionFloorState(floor_version="2026.7.5"), hi_env).floor_version
    assert app_floor == advanced.floor_version


def test_apply_floor_bump_raises_floor():
    bump = sign_floor_bump(
        FloorBump(deployment_scope="*", floor_version="2026.8.0", issued_at="2026-07-12T00:00:00+00:00"),
        REL_PRIV,
    )
    assert bv.verify_floor_bump(bump.model_dump(), release_public_key_b64=REL_PUB, expected_deployment_id="dep_a") == []
    raised = bv.apply_floor_bump(bv.FloorState(floor_version="2026.7.2"), bump.model_dump())
    assert raised.floor_version == "2026.8.0"
    # a bump below the current floor is ignored (raise-only)
    lower = sign_floor_bump(
        FloorBump(deployment_scope="*", floor_version="2026.1.0", issued_at="2026-07-12T00:00:00+00:00"), REL_PRIV)
    assert bv.apply_floor_bump(bv.FloorState(floor_version="2026.8.0"), lower.model_dump()).floor_version == "2026.8.0"
    # a tampered bump signature is rejected
    bad = bump.model_copy(update={"signature": "!!!"})
    assert bv.verify_floor_bump(bad.model_dump(), release_public_key_b64=REL_PUB, expected_deployment_id="dep_a") == ["signature_invalid"]
    # scope mismatch
    scoped = sign_floor_bump(
        FloorBump(deployment_scope="dep_other", floor_version="2026.9.0", issued_at="2026-07-12T00:00:00+00:00"),
        REL_PRIV)
    assert bv.verify_floor_bump(scoped.model_dump(), release_public_key_b64=REL_PUB, expected_deployment_id="dep_a") == ["scope_mismatch"]

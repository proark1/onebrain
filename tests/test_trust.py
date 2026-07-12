"""Trust primitives: Ed25519 signing, release canonical payloads, the registry
allowlist, the two-key desired-state envelope (D-11), and floor-bump revocation."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.trust.envelope import (
    DesiredStateEnvelope,
    FloorBump,
    SignedReleaseBlock,
    VersionFloorState,
    advance_floor,
    apply_floor_bump,
    compare_versions,
    sign_desired_state,
    sign_floor_bump,
    verify_desired_state,
    verify_desired_state_multi,
    verify_floor_bump,
)
from app.trust.release import (
    canonical_release_payload,
    parse_registry_allowlist,
    release_signature_fields,
    release_signature_fields_from_body,
    sign_release,
    verify_images,
    verify_release_signature,
)
from app.trust.signing import generate_keypair, public_key_from_private, sign_payload, verify_payload

_DIGEST = "b" * 64
_GOOD_IMAGE = f"ghcr.io/proark1/onebrain-api@sha256:{_DIGEST}"
_ALLOWLIST = frozenset({"ghcr.io/proark1"})
_NOW = datetime(2026, 7, 12, 0, 30, tzinfo=timezone.utc)


def _release_fields(**overrides) -> dict:
    fields = {
        "version": "2026.07.2",
        "git_sha": "abc123",
        "modules": {"onebrain-api": "0.9.0"},
        "images": {"onebrain-api": _GOOD_IMAGE},
        "migration_from": "0018",
        "migration_to": "0019",
        "rollback_kind": "code_only",
    }
    fields.update(overrides)
    return fields


# --- signing.py ----------------------------------------------------------------

def test_keygen_sign_verify_roundtrip():
    private_key, public_key = generate_keypair()
    payload = canonical_release_payload(**_release_fields())

    signature = sign_payload(payload, private_key)

    assert verify_payload(payload, signature, public_key) is True


def test_verify_rejects_tampered_payload():
    private_key, public_key = generate_keypair()
    signature = sign_release(_release_fields(), private_key)

    tampered = _release_fields(git_sha="abc124")  # one byte flipped

    assert verify_release_signature(tampered, signature, public_key) is False


def test_verify_rejects_wrong_key():
    private_key, _ = generate_keypair()
    _, other_public = generate_keypair()
    fields = _release_fields()
    signature = sign_release(fields, private_key)

    assert verify_release_signature(fields, signature, other_public) is False


def test_verify_rejects_garbage_signature_without_raising():
    private_key, public_key = generate_keypair()
    payload = canonical_release_payload(**_release_fields())
    good_signature = sign_payload(payload, private_key)

    assert verify_payload(payload, "not-base64!", public_key) is False
    assert verify_payload(payload, "", public_key) is False
    assert verify_payload(payload, "c2hvcnQ=", public_key) is False  # wrong length
    assert verify_payload(payload, good_signature, "not-base64!") is False
    assert verify_payload(payload, good_signature, "") is False


# --- release.py ------------------------------------------------------------------

def test_canonical_payload_is_order_insensitive():
    modules_a = {"onebrain-api": "1.0", "communication-api": "2.0"}
    modules_b = {"communication-api": "2.0", "onebrain-api": "1.0"}
    images_a = {
        "onebrain-api": _GOOD_IMAGE,
        "communication-api": f"ghcr.io/proark1/communication-api@sha256:{_DIGEST}",
    }
    images_b = dict(reversed(list(images_a.items())))

    payload_a = canonical_release_payload(**_release_fields(modules=modules_a, images=images_a))
    payload_b = canonical_release_payload(**_release_fields(modules=modules_b, images=images_b))

    assert payload_a == payload_b
    raw = payload_a.decode("utf-8")
    assert '"contract":"onebrain-release.v1"' in raw
    # Mutable operator bookkeeping is outside the integrity boundary.
    assert "status" not in raw
    assert "security_notes" not in raw
    assert "rollback_plan" not in raw
    assert "signing_key_id" not in raw


def test_release_signature_fields_from_body_strips_like_persistence():
    body = {
        "version": " 2026.07.2 ",
        "git_sha": " abc123 ",
        "modules": {" onebrain-api ": " 0.9.0 "},
        "images": {" onebrain-api ": f" {_GOOD_IMAGE} "},
        "migration_from": " 0018 ",
        "migration_to": " 0019 ",
        "rollback_kind": " code_only ",
    }

    assert release_signature_fields_from_body(body) == _release_fields()


def test_parse_registry_allowlist():
    parsed = parse_registry_allowlist("ghcr.io/proark1, Registry.Example:5000/team ,,")

    assert parsed == frozenset({"ghcr.io/proark1", "registry.example:5000/team"})
    assert parse_registry_allowlist("") == frozenset()

    # A trailing '/' would silently match nothing (the prefix match appends its
    # own '/' boundary) — normalize it away; a bare '/' entry is just blank.
    slashed = parse_registry_allowlist("ghcr.io/proark1/, /")
    assert slashed == frozenset({"ghcr.io/proark1"})
    assert verify_images({"onebrain-api": _GOOD_IMAGE}, slashed) == []


def test_verify_images_allowlist():
    assert verify_images({"onebrain-api": _GOOD_IMAGE}, _ALLOWLIST) == []

    off_registry = verify_images(
        {"onebrain-api": f"docker.io/proark1/onebrain-api@sha256:{_DIGEST}"}, _ALLOWLIST)
    assert len(off_registry) == 1 and "allowlist" in off_registry[0]

    malformed = verify_images({"onebrain-api": "ghcr.io/proark1/onebrain-api:latest"}, _ALLOWLIST)
    assert len(malformed) == 1 and "digest-pinned" in malformed[0]

    unknown = verify_images({"not-a-module": _GOOD_IMAGE}, _ALLOWLIST)
    assert len(unknown) == 1 and "unknown module id" in unknown[0]

    # Empty allowlist rejects everything (fail-closed).
    assert verify_images({"onebrain-api": _GOOD_IMAGE}, frozenset()) != []


def test_verify_images_prefix_boundary():
    assert verify_images({"onebrain-api": _GOOD_IMAGE}, _ALLOWLIST) == []

    # Path-segment boundary: a prefix must not leak into sibling orgs.
    sibling_org = verify_images(
        {"onebrain-api": f"ghcr.io/proark1x/evil@sha256:{_DIGEST}"}, _ALLOWLIST)
    assert len(sibling_org) == 1 and "allowlist" in sibling_org[0]

    other_org = verify_images(
        {"onebrain-api": f"ghcr.io/attacker/onebrain-api@sha256:{_DIGEST}"}, _ALLOWLIST)
    assert len(other_org) == 1 and "allowlist" in other_org[0]

    # An exact-equality (full-repo) entry matches itself.
    exact = frozenset({"ghcr.io/proark1/onebrain-api"})
    assert verify_images({"onebrain-api": _GOOD_IMAGE}, exact) == []


# --- envelope.py: two-key desired state (D-11) -----------------------------------

def _signed_block(release_private_key: str, **overrides) -> SignedReleaseBlock:
    fields = _release_fields(**overrides)
    signature = sign_release(fields, release_private_key)
    return SignedReleaseBlock(**fields, signature=signature)


def _envelope(block: SignedReleaseBlock, mc_private_key: str, **overrides) -> DesiredStateEnvelope:
    kwargs = dict(
        deployment_id="dep_a",
        release=block,
        nonce="nonce-0001",
        issued_at="2026-07-12T00:00:00+00:00",
        expires_at="2026-07-12T01:00:00+00:00",
    )
    kwargs.update(overrides)
    return sign_desired_state(DesiredStateEnvelope(**kwargs), mc_private_key)


def _verify(envelope, mc_public_key, release_public_key, *, deployment_id="dep_a",
            now=_NOW, floor=None, allowlist=_ALLOWLIST):
    return verify_desired_state(
        envelope,
        desired_state_public_key_b64=mc_public_key,
        release_public_key_b64=release_public_key,
        expected_deployment_id=deployment_id,
        now=now,
        floor_state=floor or VersionFloorState(),
        registry_allowlist=allowlist,
    )


@pytest.fixture()
def keys():
    release_private, release_public = generate_keypair()
    mc_private, mc_public = generate_keypair()
    return release_private, release_public, mc_private, mc_public


def test_envelope_two_key_roundtrip(keys):
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(_signed_block(release_private), mc_private)

    assert _verify(envelope, mc_public, release_public) == []

    # Any wrapper tamper after MC signing dies FIRST, before every other gate.
    tampered = envelope.model_copy(update={"deployment_id": "dep_b"})
    assert _verify(tampered, mc_public, release_public) == ["envelope_signature_invalid"]


def test_envelope_rejects_release_not_offline_signed(keys):
    """B1 — the compromised-MC case: a correctly MC-signed wrapper around a
    release block the OFFLINE key never signed must be refused."""
    release_private, release_public, mc_private, mc_public = keys

    # Images swapped to malware AFTER offline signing; MC re-wraps "helpfully".
    good_signature = sign_release(_release_fields(), release_private)
    swapped = SignedReleaseBlock(
        **_release_fields(images={"onebrain-api": f"ghcr.io/proark1/malware@sha256:{_DIGEST}"}),
        signature=good_signature,
    )
    assert _verify(_envelope(swapped, mc_private), mc_public, release_public) == [
        "release_signature_invalid"
    ]

    # Block signed by the WRONG key (e.g. the MC key itself).
    mc_signed_block = _signed_block(mc_private)
    assert _verify(_envelope(mc_signed_block, mc_private), mc_public, release_public) == [
        "release_signature_invalid"
    ]

    # Garbage in the signature slot (schema forbids empty — garbage is the
    # closest representable "missing signature").
    garbage = SignedReleaseBlock(**_release_fields(), signature="bm90LWEtc2ln")
    assert _verify(_envelope(garbage, mc_private), mc_public, release_public) == [
        "release_signature_invalid"
    ]


def test_envelope_rejects_wrong_deployment(keys):
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(_signed_block(release_private), mc_private, deployment_id="dep_b")

    assert _verify(envelope, mc_public, release_public) == ["deployment_id_mismatch"]


def test_envelope_rejects_expired(keys):
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(_signed_block(release_private), mc_private)

    late = datetime(2026, 7, 12, 2, 0, tzinfo=timezone.utc)
    assert _verify(envelope, mc_public, release_public, now=late) == ["expired"]

    malformed = _envelope(_signed_block(release_private), mc_private, expires_at="not-a-date")
    assert _verify(malformed, mc_public, release_public) == ["malformed_expiry"]


def test_envelope_rejects_reused_nonce(keys):
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(_signed_block(release_private), mc_private)

    replayed = VersionFloorState(seen_nonces=("nonce-0001",))
    assert _verify(envelope, mc_public, release_public, floor=replayed) == ["nonce_reused"]


def test_envelope_rejects_version_below_floor(keys):
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(_signed_block(release_private), mc_private)  # release 2026.07.2

    floored = VersionFloorState(floor_version="2026.07.5")
    assert _verify(envelope, mc_public, release_public, floor=floored) == ["version_below_floor"]

    # Empty floor = no floor yet -> passes.
    assert _verify(envelope, mc_public, release_public) == []


def test_envelope_fails_closed_on_uncomparable_version(keys):
    release_private, release_public, mc_private, mc_public = keys

    envelope = _envelope(_signed_block(release_private), mc_private)  # release 2026.07.2
    weird_floor = VersionFloorState(floor_version="abc")
    assert _verify(envelope, mc_public, release_public, floor=weird_floor) == [
        "version_not_comparable"
    ]

    weird_release = _envelope(_signed_block(release_private, version="abc"), mc_private)
    floored = VersionFloorState(floor_version="2026.07.1")
    assert _verify(weird_release, mc_public, release_public, floor=floored) == [
        "version_not_comparable"
    ]


def test_envelope_rejects_off_allowlist_images(keys):
    """The box's LOCAL allowlist is the backstop even for offline-signed blocks."""
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(
        _signed_block(release_private,
                      images={"onebrain-api": f"ghcr.io/attacker/onebrain-api@sha256:{_DIGEST}"}),
        mc_private,
    )

    errors = _verify(envelope, mc_public, release_public)

    assert len(errors) == 1
    assert errors[0].startswith("images_invalid:")


def test_envelope_rejects_empty_images_map(keys):
    """A signed release block whose images map is EMPTY pins nothing — the
    envelope would 'verify' while asserting no content at all. Refused."""
    release_private, release_public, mc_private, mc_public = keys
    envelope = _envelope(_signed_block(release_private, images={}), mc_private)

    errors = _verify(envelope, mc_public, release_public)

    assert len(errors) == 1
    assert errors[0].startswith("images_invalid:")
    assert "empty" in errors[0]


def test_advance_floor_monotonic_and_records_nonce(keys):
    release_private, _, mc_private, _ = keys
    state = VersionFloorState()

    first = _envelope(_signed_block(release_private), mc_private)  # release 2026.07.2
    state = advance_floor(state, first)
    assert state.floor_version == "2026.07.2"
    assert "nonce-0001" in state.seen_nonces

    lower = _envelope(_signed_block(release_private, version="2026.07.1"), mc_private,
                      nonce="nonce-0002")
    state = advance_floor(state, lower)
    assert state.floor_version == "2026.07.2"  # RAISE-ONLY
    assert "nonce-0002" in state.seen_nonces

    bumped = _envelope(_signed_block(release_private, version="2026.07.3"), mc_private,
                       nonce="nonce-0003", version_floor="2026.08.0")
    state = advance_floor(state, bumped)
    assert state.floor_version == "2026.08.0"  # envelope floor above release version wins

    # Nonce history is bounded at 100, oldest dropped.
    for index in range(105):
        beat = DesiredStateEnvelope(
            deployment_id="dep_a",
            release=_signed_block(release_private),
            nonce=f"nonce-x{index:04d}",
            issued_at="2026-07-12T00:00:00+00:00",
            expires_at="2026-07-12T01:00:00+00:00",
        )
        state = advance_floor(state, beat)
    assert len(state.seen_nonces) == 100
    assert "nonce-0001" not in state.seen_nonces
    assert "nonce-x0104" in state.seen_nonces


def test_envelope_version_floor_is_raise_only(keys):
    """B4 — envelope.version_floor may only ever RAISE the local floor, and only
    via advance_floor after a successful apply (fence + smoke)."""
    release_private, release_public, mc_private, mc_public = keys
    local = VersionFloorState(floor_version="2026.07.2")

    below = _envelope(_signed_block(release_private), mc_private, version_floor="2026.07.1")
    # Verification ignores the envelope floor entirely (local floor rules)...
    assert _verify(below, mc_public, release_public, floor=local) == []
    # ...and applying it can never LOWER the local floor.
    after = advance_floor(local, below)
    assert after.floor_version == "2026.07.2"

    above = _envelope(_signed_block(release_private), mc_private, version_floor="2026.09.0",
                      nonce="nonce-0009")
    # verify_desired_state is pure: no floor movement before apply.
    assert _verify(above, mc_public, release_public, floor=local) == []
    raised = advance_floor(local, above)
    assert raised.floor_version == "2026.09.0"


def test_envelope_schema_closed(keys):
    release_private, _, _, _ = keys
    block = _signed_block(release_private)

    with pytest.raises(ValidationError):
        DesiredStateEnvelope(
            deployment_id="dep_a", release=block, nonce="nonce-0001",
            issued_at="2026-07-12T00:00:00+00:00", expires_at="2026-07-12T01:00:00+00:00",
            surprise_field="x",
        )
    with pytest.raises(ValidationError):
        SignedReleaseBlock(**_release_fields(), signature="c2ln", surprise_field="x")
    with pytest.raises(ValidationError):
        FloorBump(floor_version="1.0", issued_at="2026-07-12T00:00:00+00:00", surprise_field="x")


# --- envelope.py: floor bump revocation (B3) --------------------------------------

def test_floor_bump_sign_verify_roundtrip(keys):
    release_private, release_public, _, _ = keys
    bump = FloorBump(floor_version="2026.07.5", issued_at="2026-07-12T00:00:00+00:00")

    signed = sign_floor_bump(bump, release_private)

    assert verify_floor_bump(signed, release_public_key_b64=release_public,
                             expected_deployment_id="dep_a") == []
    # Scoped bump: matching deployment passes, "*" passes.
    scoped = sign_floor_bump(
        FloorBump(deployment_scope="dep_a", floor_version="2026.07.5",
                  issued_at="2026-07-12T00:00:00+00:00"),
        release_private,
    )
    assert verify_floor_bump(scoped, release_public_key_b64=release_public,
                             expected_deployment_id="dep_a") == []


def test_floor_bump_wrong_key_rejected(keys):
    release_private, _, mc_private, mc_public = keys
    bump = FloorBump(floor_version="2026.07.5", issued_at="2026-07-12T00:00:00+00:00")

    signed_with_mc_key = sign_floor_bump(bump, mc_private)
    _, unrelated_public = generate_keypair()

    assert verify_floor_bump(sign_floor_bump(bump, release_private),
                             release_public_key_b64=mc_public,
                             expected_deployment_id="dep_a") == ["signature_invalid"]
    assert verify_floor_bump(signed_with_mc_key,
                             release_public_key_b64=unrelated_public,
                             expected_deployment_id="dep_a") == ["signature_invalid"]


def test_floor_bump_scope_mismatch(keys):
    release_private, release_public, _, _ = keys
    signed = sign_floor_bump(
        FloorBump(deployment_scope="dep_b", floor_version="2026.07.5",
                  issued_at="2026-07-12T00:00:00+00:00"),
        release_private,
    )

    assert verify_floor_bump(signed, release_public_key_b64=release_public,
                             expected_deployment_id="dep_a") == ["scope_mismatch"]


def test_apply_floor_bump_raise_only():
    state = VersionFloorState(floor_version="2026.07.5")

    below = FloorBump(floor_version="2026.07.1", issued_at="t")
    assert apply_floor_bump(state, below).floor_version == "2026.07.5"

    uncomparable = FloorBump(floor_version="abc", issued_at="t")
    assert apply_floor_bump(state, uncomparable).floor_version == "2026.07.5"

    above = FloorBump(floor_version="2026.08.0", issued_at="t")
    assert apply_floor_bump(state, above).floor_version == "2026.08.0"

    # From an empty floor any comparable version establishes the floor.
    assert apply_floor_bump(VersionFloorState(), above).floor_version == "2026.08.0"
    assert apply_floor_bump(VersionFloorState(), uncomparable).floor_version == ""


# --- version comparator (D-9) ------------------------------------------------------

def test_compare_versions():
    assert compare_versions("1.2", "1.2") == 0
    assert compare_versions("1.2", "1.2.0") == 0  # shorter zero-padded
    assert compare_versions("1.2.0", "1.2") == 0
    assert compare_versions("1.10", "1.9") == 1
    assert compare_versions("2026.07.1", "2026.07.2") == -1
    assert compare_versions("2026.8", "2026.07.9") == 1
    assert compare_versions("abc", "1") is None
    assert compare_versions("1", "1.x") is None
    assert compare_versions("", "1") is None
    assert compare_versions("1.2-rc1", "1.2") is None  # no semver suffixes in P0

    # Segments are ASCII [0-9]+ only — int()'s laxness never sneaks a
    # non-canonical string through the version grammar (fail-closed).
    assert compare_versions(" 1", "1") is None          # whitespace
    assert compare_versions("1. 2", "1.2") is None
    assert compare_versions("+1", "1") is None          # sign characters
    assert compare_versions("1.-2", "1.2") is None
    assert compare_versions("١.٢", "1.2") is None  # unicode digits
    assert compare_versions("1_0", "10") is None        # underscore separators
    # Leading zeros remain the house calver grammar ("07" == month 7).
    assert compare_versions("2026.07.1", "2026.7.1") == 0


# --- offline CLI --------------------------------------------------------------------

def _cli_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sign_release.py"
    spec = importlib.util.spec_from_file_location("sign_release_cli", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_sign_release_cli_signs_stripped_persisted_values(tmp_path, capsys):
    """A6 end to end: the CLI signs the STRIPPED values create_release persists,
    so the stored row re-verifies — not just the raw request file."""
    cli = _cli_module()
    private_key, public_key = generate_keypair()
    key_file = tmp_path / "release.key"
    key_file.write_text(private_key + "\n", encoding="utf-8")
    manifest_file = tmp_path / "release.json"
    manifest_file.write_text(json.dumps({
        "version": " 2026.07.2 ",
        "git_sha": " abc123 ",
        "modules": {" onebrain-api ": " 0.9.0 "},
        "images": {" onebrain-api ": f" {_GOOD_IMAGE} "},
        "migration_from": "0018",
        "migration_to": "0019",
        "rollback_kind": " code_only ",
        "security_notes": "operator note stays",
    }), encoding="utf-8")

    exit_code = cli.main([
        "sign", "--manifest", str(manifest_file),
        "--private-key-file", str(key_file), "--key-id", "release-2026",
    ])

    assert exit_code == 0
    signed = json.loads(capsys.readouterr().out)
    assert signed["version"] == "2026.07.2"
    assert signed["modules"] == {"onebrain-api": "0.9.0"}
    assert signed["images"] == {"onebrain-api": _GOOD_IMAGE}
    assert signed["rollback_kind"] == "code_only"
    assert signed["signing_key_id"] == "release-2026"
    assert signed["security_notes"] == "operator note stays"
    assert verify_release_signature(
        release_signature_fields_from_body(signed), signed["signature"], public_key) is True


def test_sign_release_cli_keygen_and_bump_floor(tmp_path, capsys):
    cli = _cli_module()

    assert cli.main(["keygen"]) == 0
    keys_out = json.loads(capsys.readouterr().out)
    assert set(keys_out) == {"private_key_b64", "public_key_b64"}

    key_file = tmp_path / "release.key"
    key_file.write_text(keys_out["private_key_b64"], encoding="utf-8")

    assert cli.main(["bump-floor", "--floor-version", "2026.07.5",
                     "--private-key-file", str(key_file)]) == 0
    bump_out = json.loads(capsys.readouterr().out)
    bump = FloorBump(**bump_out)
    assert bump.contract == "onebrain-floor.v1"
    assert bump.deployment_scope == "*"
    assert verify_floor_bump(bump, release_public_key_b64=keys_out["public_key_b64"],
                             expected_deployment_id="dep_any") == []


def test_release_signature_fields_adapter_matches_stored_shape():
    """release_signature_fields over a persisted-shaped object equals the
    from_body normalization of the raw request — the A6 invariant."""
    from app.controlplane.base import ReleaseManifest

    body = {
        "version": " 2026.07.2 ",
        "git_sha": " abc123 ",
        "modules": {" onebrain-api ": " 0.9.0 "},
        "images": {" onebrain-api ": f" {_GOOD_IMAGE} "},
        "migration_from": " 0018 ",
        "migration_to": " 0019 ",
        "rollback_kind": " code_only ",
    }
    fields = release_signature_fields_from_body(body)
    stored = ReleaseManifest(
        version=fields["version"], git_sha=fields["git_sha"], modules=fields["modules"],
        images=fields["images"], migration_from=fields["migration_from"],
        migration_to=fields["migration_to"], rollback_kind=fields["rollback_kind"],
    )

    assert release_signature_fields(stored) == fields


# --- P5-02: wrapper-key derive + rotation-tolerant multi-key verify -----------

def test_public_key_from_private_matches_generate_keypair():
    private_key, public_key = generate_keypair()
    assert public_key_from_private(private_key) == public_key


def _multi_verify(envelope, keys_list, release_public_key, *, floor_state=None, now=_NOW):
    return verify_desired_state_multi(
        envelope, desired_state_public_keys=keys_list,
        release_public_key_b64=release_public_key, expected_deployment_id="dep_a",
        now=now, floor_state=floor_state or VersionFloorState(), registry_allowlist=_ALLOWLIST)


def test_verify_multi_accepts_when_second_key_matches(keys):
    release_private, release_public, mc_private, mc_public = keys
    _other_priv, other_public = generate_keypair()
    env = _envelope(_signed_block(release_private), mc_private)
    # Signed by mc_private; the box holds [other, mc] -> the SECOND key accepts.
    assert _multi_verify(env, [other_public, mc_public], release_public) == []


def test_verify_multi_rejects_when_no_key_matches_with_ordered_last_error(keys):
    release_private, release_public, mc_private, _mc_public = keys
    _p1, other1 = generate_keypair()
    _p2, other2 = generate_keypair()
    env = _envelope(_signed_block(release_private), mc_private)
    # Neither key verifies the wrapper -> the LAST attempt's ordered error.
    assert _multi_verify(env, [other1, other2], release_public) == ["envelope_signature_invalid"]


def test_verify_multi_single_key_matches_single_key_byte_for_byte(keys):
    # Parity/inertness: a one-element list is identical to verify_desired_state.
    release_private, release_public, mc_private, mc_public = keys
    env = _envelope(_signed_block(release_private), mc_private)
    single = _verify(env, mc_public, release_public)
    multi = _multi_verify(env, [mc_public], release_public)
    assert multi == single == []
    # A later gate (below-floor) yields the SAME ordered code on both paths.
    low = _envelope(_signed_block(release_private, version="2026.07.1"), mc_private)
    fs = VersionFloorState(floor_version="2026.07.9")
    assert (_multi_verify(low, [mc_public], release_public, floor_state=fs)
            == _verify(low, mc_public, release_public, floor=fs)
            == ["version_below_floor"])


def test_verify_multi_empty_list_fails_closed(keys):
    release_private, release_public, mc_private, _mc_public = keys
    env = _envelope(_signed_block(release_private), mc_private)
    # An empty/whitespace key list falls back to a single '' key -> fail-closed.
    assert _multi_verify(env, [], release_public) == ["envelope_signature_invalid"]
    assert _multi_verify(env, ["", "   "], release_public) == ["envelope_signature_invalid"]


def test_rotation_walk_overlap_prevents_breakage():
    """The whole point of the overlap SET: a private-key swap never strands a box,
    because the box accepts the old key until it re-fetches the [old,new] set."""
    release_private, release_public = generate_keypair()
    old_priv, old_pub = generate_keypair()
    new_priv, new_pub = generate_keypair()
    block = _signed_block(release_private)

    env_old = _envelope(block, old_priv)
    env_new = _envelope(block, new_priv)
    # 1. MC signs with OLD; box holds [old_pub] -> accept.
    assert _multi_verify(env_old, [old_pub], release_public) == []
    # 2. MC swaps to NEW while the box STILL holds only [old_pub] -> reject (the window
    #    the epoch-bump re-fetch closes).
    assert _multi_verify(env_new, [old_pub], release_public) == ["envelope_signature_invalid"]
    # 3. Box re-fetches the overlap set [old_pub,new_pub] -> the NEW envelope accepts,
    #    and the OLD one still does too (no flag day).
    assert _multi_verify(env_new, [old_pub, new_pub], release_public) == []
    assert _multi_verify(env_old, [old_pub, new_pub], release_public) == []

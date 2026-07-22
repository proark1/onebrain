"""The signed desired-state envelope a box will verify before converging
(architecture §3b/§3e). Two-key trust chain (D-11): the embedded release block
is signed by the OFFLINE release key (never on MC); the thin wrapper is signed
by the MC-held online desired-state key, whose compromise can only choose WHICH
offline-signed promoted release a box runs. P0 ships the SCHEMA + verification
logic only — MC-side computation is P2, on-box floor persistence is P3, so
floor/nonce state, keys, and the clock are injected. Key selection NEVER honors
key-id claims in the payload (B8) — only locally-configured keys are tried.

The envelope schema is provisional pending P2: the verification order and error
codes below are contract; fields may extend additively."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.trust.release import release_signature_fields, verify_images, verify_release_signature
from app.trust.signing import sign_payload, verify_payload

DESIRED_STATE_CONTRACT = "desired-state.v1"
FLOOR_BUMP_CONTRACT = "onebrain-floor.v1"

_NONCE_HISTORY_LIMIT = 100


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class SignedReleaseBlock(BaseModel):
    """Byte-for-byte copy of the offline-signed release-manifest fields (the
    canonical_release_payload kwargs) PLUS the offline release signature. The
    box verifies THIS against the offline release public key — MC cannot forge
    it, so a compromised MC can never introduce an unsigned image (B1)."""
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    git_sha: str = Field(default="", max_length=64)
    modules: Dict[str, str] = Field(default_factory=dict)
    images: Dict[str, str] = Field(default_factory=dict)
    migration_from: str = Field(default="", max_length=64)
    migration_to: str = Field(default="", max_length=64)
    rollback_kind: str = Field(default="", max_length=32)
    signature: str = Field(min_length=1, max_length=512)    # offline release signature — REQUIRED here


class DesiredStateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: Literal["desired-state.v1"] = DESIRED_STATE_CONTRACT
    deployment_id: str = Field(min_length=1, max_length=120)
    release: SignedReleaseBlock                             # target version/images/kind live HERE, signed offline
    version_floor: str = Field(default="", max_length=64)   # RAISE-ONLY (B4): may raise the box's floor, never lower it
    nonce: str = Field(min_length=8, max_length=64)
    issued_at: str = Field(min_length=1, max_length=40)     # ISO-8601
    expires_at: str = Field(min_length=1, max_length=40)    # ISO-8601
    envelope_key_id: str = Field(default="", max_length=64) # bookkeeping only — never selects a key (B8)
    envelope_signature: str = Field(default="", max_length=512)  # MC desired-state key (D-11)


def canonical_envelope_payload(envelope: DesiredStateEnvelope) -> bytes:
    """Canonical JSON of every field EXCEPT envelope_signature. The embedded
    release block — INCLUDING its offline signature — is inside the wrapper
    payload, so the wrapper signature also pins which signed release was offered."""
    payload = envelope.model_dump()
    payload.pop("envelope_signature", None)
    return _canonical_json(payload)


def sign_desired_state(envelope: DesiredStateEnvelope, desired_state_private_key_b64: str) -> DesiredStateEnvelope:
    """P2 MC-side tooling (ONLINE desired-state key — never the release key):
    returns a copy with envelope_signature set."""
    signature = sign_payload(canonical_envelope_payload(envelope), desired_state_private_key_b64)
    return envelope.model_copy(update={"envelope_signature": signature})


# A version segment is ASCII digits ONLY. int()'s laxness — surrounding
# whitespace, '+'/'-' signs, unicode digits ('١'), '_' separators — is
# rejected as a non-integer segment (fail-closed per the docstring rule).
# Leading zeros stay valid: the house calver grammar ("2026.07.1") zero-pads
# months, and "07" == "7" under integer comparison.
_VERSION_SEGMENT_RE = re.compile(r"[0-9]+")


def compare_versions(a: str, b: str) -> Optional[int]:
    """-1/0/1 on dot-separated integer versions (shorter zero-padded);
    None when either side has a non-integer segment (caller fails closed).
    Segments must be ASCII [0-9]+ — see _VERSION_SEGMENT_RE."""
    segments_a = (a or "").split(".")
    segments_b = (b or "").split(".")
    if not all(_VERSION_SEGMENT_RE.fullmatch(segment) for segment in segments_a + segments_b):
        return None
    parts_a = [int(segment) for segment in segments_a]
    parts_b = [int(segment) for segment in segments_b]
    width = max(len(parts_a), len(parts_b))
    parts_a += [0] * (width - len(parts_a))
    parts_b += [0] * (width - len(parts_b))
    return (parts_a > parts_b) - (parts_a < parts_b)


def _comparable(version: str) -> bool:
    """Grammar check via self-comparison: non-integer segments fail closed."""
    return compare_versions(version, version) is not None


class VersionFloorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor_version: str = ""
    seen_nonces: tuple[str, ...] = ()


def verify_desired_state(
    envelope: DesiredStateEnvelope, *,
    desired_state_public_key_b64: str,   # MC wrapper key (online, D-11)
    release_public_key_b64: str,         # OFFLINE release key
    expected_deployment_id: str,
    now,                       # datetime, injected (aware; naive treated as UTC)
    floor_state: VersionFloorState,
    registry_allowlist: frozenset[str],  # the box's cloud-init-baked LOCAL copy — never envelope-supplied (B2)
) -> list[str]:
    """Ordered error codes; empty list = envelope accepted:
    envelope_signature_invalid | release_signature_invalid | deployment_id_mismatch |
    expired | malformed_expiry | nonce_reused | version_not_comparable |
    version_below_floor | images_invalid:<detail>
    Envelope signature is checked FIRST (nothing else is trusted before it);
    release signature SECOND (verify_release_signature over the embedded block's
    fields against the OFFLINE key — the check that stops a compromised MC from
    shipping unsigned images, B1). Verification stops at the first failing gate
    (no later gate ever runs on unverified data). The box's floor is the
    injected floor_state.floor_version — NEVER envelope.version_floor (B4: a
    builder who wires the floor from the envelope hands floor control to MC).
    version_below_floor uses compare_versions(envelope.release.version,
    floor_state.floor_version) < 0 (empty floor = no floor yet -> passes).
    images checked via trust.release.verify_images; an EMPTY images map is
    images_invalid too — an envelope that pins no images verifies nothing,
    so accepting it would let a box converge on unpinned content. Keys are
    the locally configured ones only — envelope_key_id is never honored (B8)."""
    if not verify_payload(
        canonical_envelope_payload(envelope), envelope.envelope_signature, desired_state_public_key_b64
    ):
        return ["envelope_signature_invalid"]
    block = envelope.release
    if not verify_release_signature(
        release_signature_fields(block), block.signature, release_public_key_b64
    ):
        return ["release_signature_invalid"]
    if envelope.deployment_id != expected_deployment_id:
        return ["deployment_id_mismatch"]
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        expires_at = datetime.fromisoformat(envelope.expires_at)
    except ValueError:
        return ["malformed_expiry"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        return ["expired"]
    if envelope.nonce in floor_state.seen_nonces:
        return ["nonce_reused"]
    if floor_state.floor_version:
        comparison = compare_versions(block.version, floor_state.floor_version)
        if comparison is None:
            return ["version_not_comparable"]
        if comparison < 0:
            return ["version_below_floor"]
    if not block.images:
        return ["images_invalid:empty images map (envelope pins nothing)"]
    image_errors = verify_images(dict(block.images), registry_allowlist)
    if image_errors:
        return [f"images_invalid:{error}" for error in image_errors]
    return []


def verify_desired_state_multi(
    envelope: DesiredStateEnvelope, *, desired_state_public_keys: list[str],
    release_public_keys: list[str] | None = None, **kw
) -> list[str]:
    """Rotation-tolerant wrapper (P5-02, extended): try each candidate WRAPPER public
    key AND each candidate RELEASE public key, returning [] on the FIRST fully-accepted
    combination, else the ordered error list from the LAST attempt (an all-reject still
    yields an ordered code). An empty wrapper set falls back to a single '' key (which
    fails envelope_signature_invalid — fail-closed); an empty release set falls back to
    the single release_public_key_b64 in **kw, so a box carrying only the legacy singular
    UPDATE_RELEASE_PUBLIC_KEY behaves EXACTLY as before (one release key, wrapper loop).

    PURE ADDITIVE: verify_desired_state is unchanged (single wrapper key, single release
    key, frozen), so every existing trust test passes byte-for-byte. Two overlap sets now
    live on the box (delivered via the bundle / baked in box.env), never inside the
    envelope — MC still signs with exactly one private wrapper key. The release set is
    what lets Mission Control's OWN box trust BOTH the offline production key and the CI
    development key at once; customer boxes keep the singular production key untouched."""
    wrapper_keys = [k for k in (desired_state_public_keys or []) if k and k.strip()]
    if not wrapper_keys:
        wrapper_keys = [""]
    release_keys = [k for k in (release_public_keys or []) if k and k.strip()]
    if not release_keys:
        release_keys = [kw.get("release_public_key_b64", "")]
    errors: list[str] = ["envelope_signature_invalid"]
    for release_key in release_keys:
        attempt_kw = {**kw, "release_public_key_b64": release_key}
        for wrapper_key in wrapper_keys:
            errors = verify_desired_state(envelope, desired_state_public_key_b64=wrapper_key, **attempt_kw)
            if not errors:
                return []
    return errors


def advance_floor(state: VersionFloorState, envelope: DesiredStateEnvelope) -> VersionFloorState:
    """After a SUCCESSFUL APPLY — defined (B4) as fence passed AND smoke check
    passed, so a failed-smoke auto-rollback (§3e step 7) can never strand a box
    below its own floor: floor = max(current floor, envelope.release.version,
    envelope.version_floor-when-comparable). RAISE-ONLY: any envelope value
    below the current floor is ignored. Nonce recorded (keep the last 100;
    oldest dropped)."""
    floor = state.floor_version
    for candidate in (envelope.release.version, envelope.version_floor):
        if not candidate or not _comparable(candidate):
            continue
        if not floor:
            floor = candidate
            continue
        comparison = compare_versions(candidate, floor)
        if comparison is not None and comparison > 0:
            floor = candidate
    seen = (state.seen_nonces + (envelope.nonce,))[-_NONCE_HISTORY_LIMIT:]
    return VersionFloorState(floor_version=floor, seen_nonces=seen)


# --- Revocation (B3): the offline-signed floor bump ----------------------------
# `yanked` in MC's database is deliberately OUTSIDE the integrity boundary — it
# is an MC-side convenience gate only, and a compromised MC can re-wrap any
# still-signed release at or above a box's floor. THIS is the kill mechanism:
# after yanking a bad-but-signed release, the operator signs "floor := X" with
# the OFFLINE release key; boxes raise their local floor past the yanked
# version even without updating. Distribution channel is P3 (update.sh fetch);
# P0 ships the primitive + CLI so the mechanism exists before any box does.

class FloorBump(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: Literal["onebrain-floor.v1"] = FLOOR_BUMP_CONTRACT
    deployment_scope: str = Field(default="*", max_length=120)  # "*" or a deployment_id
    floor_version: str = Field(min_length=1, max_length=64)
    issued_at: str = Field(min_length=1, max_length=40)
    signature: str = Field(default="", max_length=512)          # OFFLINE release key


def canonical_floor_bump_payload(bump: FloorBump) -> bytes:
    """Canonical JSON of every field EXCEPT signature (the contract string
    inside the payload prevents cross-protocol replay, D-2)."""
    payload = bump.model_dump()
    payload.pop("signature", None)
    return _canonical_json(payload)


def sign_floor_bump(bump: FloorBump, private_key_b64: str) -> FloorBump:
    """OFFLINE ONLY (scripts/sign_release.py bump-floor)."""
    signature = sign_payload(canonical_floor_bump_payload(bump), private_key_b64)
    return bump.model_copy(update={"signature": signature})


def verify_floor_bump(bump: FloorBump, *, release_public_key_b64: str,
                      expected_deployment_id: str) -> list[str]:
    """signature_invalid | scope_mismatch | version_not_comparable (grammar via
    compare_versions against itself: non-integer segments fail closed)."""
    if not verify_payload(canonical_floor_bump_payload(bump), bump.signature, release_public_key_b64):
        return ["signature_invalid"]
    if bump.deployment_scope not in ("*", expected_deployment_id):
        return ["scope_mismatch"]
    if not _comparable(bump.floor_version):
        return ["version_not_comparable"]
    return []


def apply_floor_bump(state: VersionFloorState, bump: FloorBump) -> VersionFloorState:
    """floor = max(floor, bump.floor_version); RAISE-ONLY — a bump below the
    current floor, or an uncomparable version, leaves state unchanged."""
    candidate = bump.floor_version
    if not candidate or not _comparable(candidate):
        return state
    if state.floor_version:
        comparison = compare_versions(candidate, state.floor_version)
        if comparison is None or comparison <= 0:
            return state
    return state.model_copy(update={"floor_version": candidate})

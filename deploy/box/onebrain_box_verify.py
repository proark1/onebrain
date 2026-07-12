#!/usr/bin/env python3
"""App-free box-side verifier for the signed desired-state envelope (P1-F, H-11).

This module re-implements — WITHOUT importing `app` — the exact verification logic
of `app.trust.signing` + `app.trust.release` + `app.trust.envelope`
(`verify_desired_state`/`compare_versions`/floor+nonce). It is the box's LAST line
of defence against a compromised Mission Control: the box VERIFIES, never trusts
MC (D2). A conformance test (`tests/test_box_verify.py`) pins this file byte-for-byte
to `app.trust.envelope` so the two can never drift.

Only stdlib + `cryptography` (raw Ed25519) — no `app`, no pydantic, no network — so
the recovery channel does not share the app container's failure domain.

CLI:
  verify            envelope JSON on stdin; trust anchors from env; on success prints
                    the validated target JSON to stdout (exit 0); on failure prints the
                    ordered error code to stderr (exit 2).
  record-apply      envelope JSON on stdin; after a SUCCESSFUL apply, advance the local
                    floor + record the nonce (raise-only), persisting floor_state.json.
  apply-floor-bump  signed FloorBump JSON on stdin; verify against the release key and
                    raise the local floor (the yank/revocation kill mechanism).

Env: UPDATE_DESIRED_STATE_PUBLIC_KEY, UPDATE_RELEASE_PUBLIC_KEY,
     UPDATE_REGISTRY_ALLOWLIST, ONEBRAIN_DEPLOYMENT_ID, UPDATE_DATA_DIR.
Floor state: ${UPDATE_DATA_DIR}/onebrain_update/floor_state.json.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# --- domain constants (identical to app.trust) -------------------------------
RELEASE_SIGNING_CONTRACT = "onebrain-release.v1"
DESIRED_STATE_CONTRACT = "desired-state.v1"
FLOOR_BUMP_CONTRACT = "onebrain-floor.v1"
_NONCE_HISTORY_LIMIT = 100

# The single module vocabulary (app.controlplane.base.MODULE_IDS).
MODULE_IDS = frozenset({
    "onebrain-api",
    "onebrain-admin-ui",
    "onebrain-workers",
    "assistant-service",
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
})

# registry/repo@sha256:<64 hex> (app.controlplane.base.IMAGE_DIGEST_RE).
IMAGE_DIGEST_RE = re.compile(
    r"^(?P<registry>[a-z0-9][a-z0-9.\-]*(?::\d+)?)/(?P<repo>[a-z0-9][a-z0-9._\-/]*)@sha256:(?P<digest>[0-9a-f]{64})$"
)
# Version segment: ASCII digits ONLY (app.trust.envelope._VERSION_SEGMENT_RE).
_VERSION_SEGMENT_RE = re.compile(r"[0-9]+")


# --- signing (identical to app.trust.signing.verify_payload) -----------------
def verify_payload(payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """True iff the signature verifies. Never raises: malformed key/signature/
    base64 returns False (fail-closed)."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), payload)
        return True
    except Exception:
        return False


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


# --- release payload (identical to app.trust.release) ------------------------
def canonical_release_payload(*, version, git_sha, modules, images,
                              migration_from, migration_to, rollback_kind) -> bytes:
    payload = {
        "contract": RELEASE_SIGNING_CONTRACT,
        "version": version,
        "git_sha": git_sha,
        "modules": {str(k): str(v) for k, v in (modules or {}).items()},
        "images": {str(k): str(v) for k, v in (images or {}).items()},
        "migration_from": migration_from,
        "migration_to": migration_to,
        "rollback_kind": rollback_kind,
    }
    return _canonical_json(payload)


def release_signature_fields(block: dict) -> dict:
    """Canonical-payload kwargs from a SignedReleaseBlock dict (defaults mirror the
    pydantic model)."""
    return {
        "version": block.get("version", ""),
        "git_sha": block.get("git_sha", ""),
        "modules": dict(block.get("modules") or {}),
        "images": dict(block.get("images") or {}),
        "migration_from": block.get("migration_from", ""),
        "migration_to": block.get("migration_to", ""),
        "rollback_kind": block.get("rollback_kind", ""),
    }


def verify_release_signature(fields: dict, signature_b64: str, public_key_b64: str) -> bool:
    return verify_payload(canonical_release_payload(**fields), signature_b64, public_key_b64)


def validate_image_ref(ref: str) -> Optional[str]:
    m = IMAGE_DIGEST_RE.match(ref or "")
    if not m:
        return f"image ref is not digest-pinned (registry/repo@sha256:...): {ref!r}"
    registry = m.group("registry")
    if "." not in registry and ":" not in registry:
        return f"image ref has no registry host: {ref!r}"
    return None


def parse_registry_allowlist(csv_value: str) -> frozenset:
    entries = (entry.strip().lower().rstrip("/") for entry in (csv_value or "").split(","))
    return frozenset(entry for entry in entries if entry)


def verify_images(images: dict, allowlist: frozenset) -> list:
    """Error strings (empty list = OK) — identical to app.trust.release.verify_images."""
    errors = []
    for module_id in sorted(images or {}):
        ref = images[module_id]
        if module_id not in MODULE_IDS:
            errors.append(f"unknown module id in images map: {module_id!r}")
            continue
        grammar_error = validate_image_ref(ref)
        if grammar_error:
            errors.append(grammar_error)
            continue
        path = ref.split("@sha256:", 1)[0].lower()
        if not any(path == entry or path.startswith(entry + "/") for entry in allowlist):
            errors.append(f"image ref not in registry allowlist: {ref!r}")
    return errors


# --- versions (identical to app.trust.envelope.compare_versions) -------------
def compare_versions(a: str, b: str) -> Optional[int]:
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
    return compare_versions(version, version) is not None


# --- envelope payload / floor state ------------------------------------------
def canonical_envelope_payload(envelope: dict) -> bytes:
    """Canonical JSON of every field EXCEPT envelope_signature (identical to
    app.trust.envelope.canonical_envelope_payload over model_dump())."""
    payload = {k: v for k, v in envelope.items() if k != "envelope_signature"}
    return _canonical_json(payload)


@dataclass(frozen=True)
class FloorState:
    floor_version: str = ""
    seen_nonces: tuple = ()


def verify_desired_state(
    envelope: dict, *,
    desired_state_public_key_b64: str,
    release_public_key_b64: str,
    expected_deployment_id: str,
    now: datetime,
    floor_state: FloorState,
    registry_allowlist: frozenset,
) -> list:
    """Ordered error codes; empty list = accepted. IDENTICAL order + codes to
    app.trust.envelope.verify_desired_state (envelope signature first, release
    signature second, then id/expiry/nonce/floor/images). The box's floor is the
    injected floor_state.floor_version — NEVER envelope.version_floor (B4)."""
    if not verify_payload(
        canonical_envelope_payload(envelope), envelope.get("envelope_signature", ""), desired_state_public_key_b64
    ):
        return ["envelope_signature_invalid"]
    block = envelope.get("release", {}) or {}
    if not verify_release_signature(
        release_signature_fields(block), block.get("signature", ""), release_public_key_b64
    ):
        return ["release_signature_invalid"]
    if envelope.get("deployment_id") != expected_deployment_id:
        return ["deployment_id_mismatch"]
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        expires_at = datetime.fromisoformat(envelope.get("expires_at", ""))
    except ValueError:
        return ["malformed_expiry"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        return ["expired"]
    if envelope.get("nonce") in floor_state.seen_nonces:
        return ["nonce_reused"]
    block_version = block.get("version", "")
    if floor_state.floor_version:
        comparison = compare_versions(block_version, floor_state.floor_version)
        if comparison is None:
            return ["version_not_comparable"]
        if comparison < 0:
            return ["version_below_floor"]
    block_images = block.get("images") or {}
    if not block_images:
        return ["images_invalid:empty images map (envelope pins nothing)"]
    image_errors = verify_images(dict(block_images), registry_allowlist)
    if image_errors:
        return [f"images_invalid:{error}" for error in image_errors]
    return []


def verify_bytes(envelope_bytes, **kwargs) -> list:
    """json.loads then verify_desired_state. Malformed JSON / non-object ->
    ['malformed_envelope'] (box-only defensive; MC always serves a valid model_dump)."""
    try:
        envelope = json.loads(envelope_bytes)
    except (ValueError, TypeError):
        return ["malformed_envelope"]
    if not isinstance(envelope, dict):
        return ["malformed_envelope"]
    return verify_desired_state(envelope, **kwargs)


def verified_target(envelope: dict) -> dict:
    """The validated target update.sh drives its pull from (A7): version + the
    digest-pinned images map + migration bounds + rollback_kind. Derived SOLELY
    from the verified release block."""
    block = envelope.get("release", {}) or {}
    return {
        "version": block.get("version", ""),
        "images": dict(block.get("images") or {}),
        "migration_from": block.get("migration_from", ""),
        "migration_to": block.get("migration_to", ""),
        "rollback_kind": block.get("rollback_kind", ""),
    }


# --- floor advance (identical to app.trust.envelope.advance_floor) -----------
def advance_floor(state: FloorState, envelope: dict) -> FloorState:
    """floor = max(current, release.version, version_floor-when-comparable); RAISE-ONLY.
    Nonce recorded (keep last 100)."""
    floor = state.floor_version
    block = envelope.get("release", {}) or {}
    for candidate in (block.get("version", ""), envelope.get("version_floor", "")):
        if not candidate or not _comparable(candidate):
            continue
        if not floor:
            floor = candidate
            continue
        comparison = compare_versions(candidate, floor)
        if comparison is not None and comparison > 0:
            floor = candidate
    seen = tuple(state.seen_nonces) + (envelope.get("nonce", ""),)
    seen = seen[-_NONCE_HISTORY_LIMIT:]
    return FloorState(floor_version=floor, seen_nonces=seen)


# --- floor bump / revocation (identical to app.trust.envelope) ---------------
def canonical_floor_bump_payload(bump: dict) -> bytes:
    payload = {k: v for k, v in bump.items() if k != "signature"}
    return _canonical_json(payload)


def verify_floor_bump(bump: dict, *, release_public_key_b64: str, expected_deployment_id: str) -> list:
    if not verify_payload(canonical_floor_bump_payload(bump), bump.get("signature", ""), release_public_key_b64):
        return ["signature_invalid"]
    if bump.get("deployment_scope", "*") not in ("*", expected_deployment_id):
        return ["scope_mismatch"]
    if not _comparable(bump.get("floor_version", "")):
        return ["version_not_comparable"]
    return []


def apply_floor_bump(state: FloorState, bump: dict) -> FloorState:
    candidate = bump.get("floor_version", "")
    if not candidate or not _comparable(candidate):
        return state
    if state.floor_version:
        comparison = compare_versions(candidate, state.floor_version)
        if comparison is None or comparison <= 0:
            return state
    return FloorState(floor_version=candidate, seen_nonces=state.seen_nonces)


# --- persistence -------------------------------------------------------------
def _floor_state_path() -> str:
    data_dir = os.environ.get("UPDATE_DATA_DIR", ".")
    return os.path.join(data_dir, "onebrain_update", "floor_state.json")


def load_floor_state(path: str) -> FloorState:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return FloorState(
            floor_version=str(data.get("floor_version", "")),
            seen_nonces=tuple(data.get("seen_nonces", ()) or ()),
        )
    except Exception:
        return FloorState()


def save_floor_state(path: str, state: FloorState) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"floor_version": state.floor_version, "seen_nonces": list(state.seen_nonces)}, fh)
    os.replace(tmp, path)


# --- CLI ---------------------------------------------------------------------
def _anchors() -> dict:
    return {
        "desired_state_public_key_b64": os.environ.get("UPDATE_DESIRED_STATE_PUBLIC_KEY", ""),
        "release_public_key_b64": os.environ.get("UPDATE_RELEASE_PUBLIC_KEY", ""),
        "expected_deployment_id": os.environ.get("ONEBRAIN_DEPLOYMENT_ID", ""),
        "registry_allowlist": parse_registry_allowlist(os.environ.get("UPDATE_REGISTRY_ALLOWLIST", "")),
    }


def _cmd_verify(raw: bytes) -> int:
    anchors = _anchors()
    floor_state = load_floor_state(_floor_state_path())
    errors = verify_bytes(
        raw, now=datetime.now(timezone.utc), floor_state=floor_state, **anchors
    )
    if errors:
        sys.stderr.write(errors[0] + "\n")
        return 2
    envelope = json.loads(raw)
    sys.stdout.write(json.dumps(verified_target(envelope), sort_keys=True) + "\n")
    return 0


def _cmd_record_apply(raw: bytes) -> int:
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise ValueError("envelope must be an object")
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"malformed_envelope: {exc}\n")
        return 2
    path = _floor_state_path()
    save_floor_state(path, advance_floor(load_floor_state(path), envelope))
    return 0


def _cmd_apply_floor_bump(raw: bytes) -> int:
    try:
        bump = json.loads(raw)
        if not isinstance(bump, dict):
            raise ValueError("bump must be an object")
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"malformed_floor_bump: {exc}\n")
        return 2
    errors = verify_floor_bump(
        bump,
        release_public_key_b64=os.environ.get("UPDATE_RELEASE_PUBLIC_KEY", ""),
        expected_deployment_id=os.environ.get("ONEBRAIN_DEPLOYMENT_ID", ""),
    )
    if errors:
        sys.stderr.write(errors[0] + "\n")
        return 2
    path = _floor_state_path()
    save_floor_state(path, apply_floor_bump(load_floor_state(path), bump))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: onebrain_box_verify.py {verify|record-apply|apply-floor-bump}\n")
        return 2
    command = argv[0]
    raw = sys.stdin.buffer.read()
    if command == "verify":
        return _cmd_verify(raw)
    if command == "record-apply":
        return _cmd_record_apply(raw)
    if command == "apply-floor-bump":
        return _cmd_apply_floor_bump(raw)
    sys.stderr.write(f"unknown command: {command}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

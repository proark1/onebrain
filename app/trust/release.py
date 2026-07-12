"""Release-manifest integrity: canonical payload, offline signature, and the
registry allowlist (a signature alone would let a compromised MC serve its own
signed image from an arbitrary registry — the allowlist closes that).

Key-selection rule (B8): verification NEVER selects a key from a manifest's
`signing_key_id` claim — that field is unsigned rotation/display bookkeeping.
Callers try only locally-configured trusted keys; an attacker-influenced key-id
hint must not steer which key is tried."""

from __future__ import annotations

import json
from typing import Mapping

from app.controlplane.base import MODULE_IDS, validate_image_ref
from app.trust.signing import sign_payload, verify_payload

RELEASE_SIGNING_CONTRACT = "onebrain-release.v1"


def canonical_release_payload(*, version: str, git_sha: str, modules: dict, images: dict,
                              migration_from: str, migration_to: str, rollback_kind: str) -> bytes:
    """Compact canonical JSON (sort_keys, no spaces, ensure_ascii) of exactly
    these fields plus {"contract": RELEASE_SIGNING_CONTRACT}. status / notes /
    rollback_plan / signing_key_id are deliberately OUTSIDE the integrity
    boundary (mutable operator bookkeeping). CONSEQUENCE (B3): 'yanked' is
    therefore unsigned MC-database state — an MC-side convenience gate a
    compromised MC can ignore. The mechanism that actually revokes a signed
    release is the offline-signed FloorBump (app/trust/envelope.py)."""
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
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sign_release(fields: dict, private_key_b64: str) -> str:
    """OFFLINE ONLY. fields = kwargs of canonical_release_payload."""
    return sign_payload(canonical_release_payload(**fields), private_key_b64)


def verify_release_signature(fields: dict, signature_b64: str, public_key_b64: str) -> bool:
    """True iff signature_b64 verifies over the canonical payload of fields.
    Never raises (fail-closed via signing.verify_payload)."""
    return verify_payload(canonical_release_payload(**fields), signature_b64, public_key_b64)


def release_signature_fields(release) -> dict:
    """Canonical-payload kwargs from a ReleaseManifest (or any object with these
    attrs). Persisted rows are already normalized, so no stripping happens here —
    use release_signature_fields_from_body for request bodies (A6)."""
    return {
        "version": release.version,
        "git_sha": release.git_sha,
        "modules": dict(release.modules or {}),
        "images": dict(release.images or {}),
        "migration_from": release.migration_from,
        "migration_to": release.migration_to,
        "rollback_kind": release.rollback_kind,
    }


def _body_field(body, name: str, default):
    if isinstance(body, Mapping):
        value = body.get(name, default)
    else:
        value = getattr(body, name, default)
    return default if value is None else value


def release_signature_fields_from_body(body) -> dict:
    """Canonical-payload kwargs from a release-creation request body (pydantic
    model or plain dict), normalized EXACTLY the way the operator create_release
    endpoint persists them (A6): stripped scalars and stripped modules/images
    keys+values. Signing or verifying anything else would record a signature
    that no longer matches the stored row, breaking every later re-verification
    (P2 envelope computation, audits)."""
    return {
        "version": str(_body_field(body, "version", "")).strip(),
        "git_sha": str(_body_field(body, "git_sha", "")).strip(),
        "modules": {str(k).strip(): str(v).strip()
                    for k, v in _body_field(body, "modules", {}).items()},
        "images": {str(k).strip(): str(v).strip()
                   for k, v in _body_field(body, "images", {}).items()},
        "migration_from": str(_body_field(body, "migration_from", "")).strip(),
        "migration_to": str(_body_field(body, "migration_to", "")).strip(),
        "rollback_kind": str(_body_field(body, "rollback_kind", "")).strip(),
    }


def parse_registry_allowlist(csv_value: str) -> frozenset[str]:
    """'ghcr.io/proark1, registry.example:5000/team' -> frozenset of lowercased
    PREFIX entries host[/org[/repo]] (blanks dropped). Repo-prefix granular (B2):
    a bare host entry on a multi-tenant registry (ghcr.io) would allowlist every
    tenant — the backstop against a compromised-MC-signed image would be porous."""
    return frozenset(
        entry.strip().lower()
        for entry in (csv_value or "").split(",")
        if entry.strip()
    )


def verify_images(images: dict[str, str], allowlist: frozenset[str]) -> list[str]:
    """Error strings (empty list = OK): unknown module id (not in MODULE_IDS),
    grammar failure (validate_image_ref), image prefix not in allowlist. Prefix
    matching (B2): an image ref matches an entry iff its 'registry/repo' path
    (everything before '@sha256:') equals the entry OR starts with entry + '/'
    — a path-segment boundary, so 'ghcr.io/proark1' allows
    ghcr.io/proark1/onebrain-api but NOT ghcr.io/proark1x/anything. Empty
    allowlist rejects everything (fail-closed).
    Box-side note (B2): the allowlist a box verifies against is its
    cloud-init-baked LOCAL copy (the injected parameter) — never a value taken
    from the envelope or an MC ack."""
    errors: list[str] = []
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

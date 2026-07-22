"""Compute the signed desired-state a box converges to (architecture §3b/§3e).

PURE: the control store, the clock, and the signing key are injected; there is no
network and no scheduler. Customer boxes receive the stored OFFLINE production
signature. Only the designated development gate receives the promotion's CI
development signature. The thin wrapper is signed by MC's online desired-state
key; every box still verifies the embedded release signature against its locally
configured trust root.

Emission is DORMANT until fleet_desired_state_private_key is configured:
sign_desired_state_for returns None, so today's fleet sees no desired-state at all.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from app.controlplane.base import ReleaseManifest, is_operator_self_deployment
from app.trust.envelope import DesiredStateEnvelope, SignedReleaseBlock, sign_desired_state
from app.trust.release import release_signature_fields
from app.trust.signing import public_key_from_private


# --- wrapper-key rotation custody (P5-02) ------------------------------------
# MC always holds exactly ONE private desired-state key and signs with it; boxes
# accept ANY public key in a delivered SET (the overlap set lives on the box, never
# inside the envelope). The accepted set is delivered via the P5-03 bundle, NOT via
# the envelope — the signing path here is unchanged (still one private key).

def active_wrapper_public_key(settings) -> str:
    """The base64 public key MC is currently signing desired-state with, derived
    from fleet_desired_state_private_key. "" when emission is disabled (no key)."""
    private_key = (settings.fleet_desired_state_private_key or "").strip()
    return public_key_from_private(private_key) if private_key else ""


def served_public_key_set(settings) -> list[str]:
    """The wrapper public keys delivered to boxes: the csv overlap set
    (fleet_desired_state_public_keys) or the singular fleet_desired_state_public_key
    fallback. Empty/whitespace entries dropped."""
    csv = settings.fleet_desired_state_public_keys or settings.fleet_desired_state_public_key or ""
    return [key.strip() for key in csv.split(",") if key.strip()]


def active_signer_in_served_set(settings) -> bool:
    """G1-1 interlock: False only when MC is signing with a wrapper key that is
    ABSENT from the set delivered to boxes — the config slip that would strand the
    whole fleet at envelope_signature_invalid (and permanently brick it if the
    served set never regains the active key). When emission is disabled (no private
    key), there is nothing to sign with and nothing to brick -> True (inert-safe)."""
    active = active_wrapper_public_key(settings)
    if not active:
        return True
    return active in served_public_key_set(settings)


def target_release_for_deployment(control_store, deployment) -> Optional[ReleaseManifest]:
    """The release a box should be running: the target of its active (non-terminal)
    rollout if one exists, else the deployment's current_version release (steady-state
    confirm). None when neither version resolves to a known release."""
    if deployment is None:
        return None
    active = control_store.list_active_rollout(deployment.id)
    version = active.target_version if active else deployment.current_version
    if not version:
        return None
    return control_store.get_release(version)


def build_desired_state(deployment, release, *, floor_version: str, now, ttl_seconds: int,
                        nonce: str, release_signature: str = "") -> DesiredStateEnvelope:
    """Assemble the UNSIGNED envelope. The SignedReleaseBlock is reconstructed from the
    STORED release fields + release.signature — Phase-3 WP4 signed that signature over
    exactly the persisted (stripped) values, so it re-verifies byte-for-byte on the box.
    Raises ValueError if release.signature is empty: an unsigned release is never offered
    to a box (the box would reject it anyway; fail loud on MC instead of shipping a dud)."""
    signature = release_signature or release.signature
    if not signature:
        raise ValueError("refusing to offer an unsigned release as desired-state")
    block = SignedReleaseBlock(**release_signature_fields(release), signature=signature)
    return DesiredStateEnvelope(
        deployment_id=deployment.id,
        release=block,
        version_floor=floor_version,
        nonce=nonce,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )


def sign_desired_state_for(control_store, deployment_id: str, *, settings, now,
                           nonce_factory=lambda: secrets.token_hex(16)) -> Optional[DesiredStateEnvelope]:
    """The serve path: resolve deployment + target release, build, and sign with
    settings.fleet_desired_state_private_key (the ONE online wrapper key MC holds, D-11).
    Returns None when emission is disabled (no wrapper key), the deployment or release is
    unknown, or the release is unsigned. version_floor = release.version (raise-only on
    the box, so serving a version can never be used to walk a box backwards)."""
    private_key = settings.fleet_desired_state_private_key
    if not private_key:
        return None
    deployment = control_store.get_deployment(deployment_id)
    if deployment is None:
        return None
    release = target_release_for_deployment(control_store, deployment)
    if release is None:
        return None
    promotion = control_store.get_release_promotion(release.version)
    operator_self = is_operator_self_deployment(deployment, settings)
    if getattr(settings, "release_promotion_required", False):
        if deployment.is_release_gate:
            if not promotion or promotion.state not in {
                "dev_pending", "dev_deploying", "dev_verified", "customer_approved",
            }:
                return None
        elif operator_self:
            # MC's OWN box tracks the development-VERIFIED tip: accept dev_verified
            # onward, never dev_pending/dev_deploying (the gate has not verified
            # those). Reinforces the auto-rollout trigger's own dev_verified gate.
            if not promotion or promotion.state not in {"dev_verified", "customer_approved"}:
                return None
        elif not promotion or promotion.state != "customer_approved":
            return None
    release_signature = release.signature
    if deployment.is_release_gate:
        if promotion and promotion.gate_deployment_id in {"", deployment.id}:
            release_signature = promotion.dev_signature
    elif operator_self:
        # Serve the CI DEVELOPMENT signature so MC self-deploys from dev_verified,
        # before any offline production signature is attached at customer approval.
        # Every registered candidate carries a dev_signature, and MC's box trusts
        # the dev key (UPDATE_RELEASE_PUBLIC_KEYS) alongside the production key.
        if promotion and promotion.dev_signature:
            release_signature = promotion.dev_signature
    if not release_signature:
        return None
    envelope = build_desired_state(
        deployment, release, floor_version=release.version, now=now,
        ttl_seconds=settings.fleet_desired_state_ttl_seconds, nonce=nonce_factory(),
        release_signature=release_signature,
    )
    return sign_desired_state(envelope, private_key)


def active_pull_attempt_id(control_store, deployment_id: str) -> str:
    """The id of the deployment's active (non-terminal) rollout, or "". Conveyed OUT OF
    BAND alongside the signed envelope (NOT inside it — DesiredStateEnvelope is
    Phase-3-frozen + extra="forbid"). The box echoes it into update_state.json so the
    reconcile tick (P4-06) can gate on UpdateReport.attempt_id == child.id (H-8). It is
    an UNSIGNED advisory hint — the box never uses it as a trust input (a wrong
    attempt_id at worst delays convergence detection, never authorizes an image)."""
    active = control_store.list_active_rollout(deployment_id)
    return active.id if active else ""

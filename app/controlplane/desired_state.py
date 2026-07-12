"""Compute the signed desired-state a box converges to (architecture §3b/§3e).

PURE: the control store, the clock, and the signing key are injected; there is no
network and no scheduler. The two-key trust chain (D-11) is assembled here but the
box VERIFIES it, never trusts it: the embedded SignedReleaseBlock carries the
release's stored OFFLINE signature (which Mission Control cannot forge, so a
compromised MC can never introduce an unsigned image), and the thin wrapper is
signed by MC's single online desired-state key (whose compromise can only choose
WHICH offline-signed, promoted release a box runs).

Emission is DORMANT until fleet_desired_state_private_key is configured:
sign_desired_state_for returns None, so today's fleet sees no desired-state at all.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from app.controlplane.base import ReleaseManifest
from app.trust.envelope import DesiredStateEnvelope, SignedReleaseBlock, sign_desired_state
from app.trust.release import release_signature_fields


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
                        nonce: str) -> DesiredStateEnvelope:
    """Assemble the UNSIGNED envelope. The SignedReleaseBlock is reconstructed from the
    STORED release fields + release.signature — Phase-3 WP4 signed that signature over
    exactly the persisted (stripped) values, so it re-verifies byte-for-byte on the box.
    Raises ValueError if release.signature is empty: an unsigned release is never offered
    to a box (the box would reject it anyway; fail loud on MC instead of shipping a dud)."""
    if not release.signature:
        raise ValueError("refusing to offer an unsigned release as desired-state")
    block = SignedReleaseBlock(**release_signature_fields(release), signature=release.signature)
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
    if release is None or not release.signature:
        return None
    envelope = build_desired_state(
        deployment, release, floor_version=release.version, now=now,
        ttl_seconds=settings.fleet_desired_state_ttl_seconds, nonce=nonce_factory(),
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

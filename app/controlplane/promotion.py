"""Release-candidate assembly, trust checks, and promotion state transitions.

The functions in this module are deterministic except for the small store wrappers.
They keep lifecycle policy out of the HTTP routes and out of persistence adapters.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Mapping, Optional

from app.controlplane.base import (
    ReleaseManifest,
    ReleasePromotion,
    ReleasePromotionEvent,
    validate_release,
)
from app.trust.release import (
    canonical_release_payload,
    release_signature_fields,
    verify_release_signature,
)


ALLOWED_PROMOTION_TRANSITIONS = {
    "dev_pending": frozenset({"dev_deploying", "yanked"}),
    "dev_deploying": frozenset({"dev_failed", "dev_verified", "yanked"}),
    "dev_failed": frozenset({"dev_deploying", "yanked"}),
    "dev_verified": frozenset({"customer_approved", "yanked"}),
    "customer_approved": frozenset({"customer_paused", "yanked"}),
    "customer_paused": frozenset({"customer_approved", "yanked"}),
    "yanked": frozenset(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decide_transition(current_state: str, next_state: str) -> str:
    """Return ``next_state`` when the transition is allowed, otherwise a stable error."""
    if next_state in ALLOWED_PROMOTION_TRANSITIONS.get(current_state, frozenset()):
        return next_state
    raise ValueError(f"invalid_promotion_transition:{current_state}:{next_state}")


def manifest_digest(release: ReleaseManifest) -> str:
    return hashlib.sha256(canonical_release_payload(**release_signature_fields(release))).hexdigest()


def immutable_manifest_matches(left: ReleaseManifest, right: ReleaseManifest) -> bool:
    return canonical_release_payload(**release_signature_fields(left)) == canonical_release_payload(
        **release_signature_fields(right)
    )


def prepare_candidate(
    *,
    version: str,
    git_sha: str,
    changed_modules: Mapping[str, str],
    changed_images: Mapping[str, str],
    baseline: Optional[ReleaseManifest],
    migration_from: str = "",
    migration_to: str = "",
    rollback_kind: str = "code_only",
    security_notes: str = "",
    rollback_plan: str = "",
) -> ReleaseManifest:
    """Merge artifacts from one green build onto the latest approved baseline."""
    modules = dict(baseline.modules if baseline else {})
    images = dict(baseline.images if baseline else {})
    modules.update({str(k).strip(): str(v).strip() for k, v in changed_modules.items()})
    images.update({str(k).strip(): str(v).strip() for k, v in changed_images.items()})
    if not baseline and set(modules) != set(images):
        raise ValueError("first candidate must provide an image for every module")
    if set(changed_modules) != set(changed_images):
        raise ValueError("changed modules and images must cover the same module ids")
    candidate = ReleaseManifest(
        version=version.strip(),
        git_sha=git_sha.strip(),
        modules=modules,
        migration_from=migration_from.strip() or (baseline.migration_to if baseline else ""),
        migration_to=migration_to.strip() or (baseline.migration_to if baseline else ""),
        security_notes=security_notes.strip(),
        rollback_plan=rollback_plan.strip(),
        status="draft",
        images=images,
        rollback_kind=rollback_kind.strip(),
    )
    validate_release(candidate)
    return candidate


def verify_development_candidate(
    release: ReleaseManifest,
    *,
    signature: str,
    development_public_key: str,
    production_public_key: str = "",
) -> None:
    if not development_public_key:
        raise ValueError("development_signature_key_missing")
    if production_public_key and development_public_key == production_public_key:
        raise ValueError("development_and_production_keys_must_differ")
    if release.signature or release.signing_key_id:
        raise ValueError("candidate_cannot_supply_production_signature")
    if not signature or not verify_release_signature(
        release_signature_fields(release), signature, development_public_key
    ):
        raise ValueError("development_signature_verification_failed")


def register_candidate(
    store,
    release: ReleaseManifest,
    *,
    dev_signature: str,
    dev_signing_key_id: str,
    development_public_key: str,
    production_public_key: str = "",
    actor: str = "ci",
) -> tuple[ReleasePromotion, bool]:
    """Persist an immutable candidate. Identical redelivery is a successful no-op."""
    validate_release(release)
    verify_development_candidate(
        release,
        signature=dev_signature,
        development_public_key=development_public_key,
        production_public_key=production_public_key,
    )
    existing = store.get_release(release.version)
    existing_promotion = store.get_release_promotion(release.version)
    if existing or existing_promotion:
        if (
            existing
            and existing_promotion
            and immutable_manifest_matches(existing, release)
            and existing_promotion.dev_signature == dev_signature
            and existing_promotion.dev_signing_key_id == dev_signing_key_id
        ):
            return existing_promotion, False
        raise ValueError("release_candidate_version_conflict")
    now = _now()
    promotion = ReleasePromotion(
        release_version=release.version,
        state="dev_pending",
        dev_signature=dev_signature,
        dev_signing_key_id=dev_signing_key_id.strip(),
        created_at=now,
        updated_at=now,
    )
    created = store.create_release_candidate(
        release,
        promotion,
        ReleasePromotionEvent(
            id="",
            release_version=release.version,
            actor=actor,
            action="candidate_registered",
            to_state="dev_pending",
            metadata={"manifest_digest": manifest_digest(release)},
            created_at=now,
        ),
    )
    return created, True


def verify_production_signature_match(
    release: ReleaseManifest,
    *,
    signature: str,
    production_public_key: str,
) -> None:
    if not production_public_key:
        raise ValueError("production_signature_key_missing")
    if not signature or not verify_release_signature(
        release_signature_fields(release), signature, production_public_key
    ):
        raise ValueError("production_signature_verification_failed")


def transition(
    store,
    version: str,
    *,
    to_state: str,
    actor: str,
    action: str,
    note: str = "",
    fields: Optional[dict] = None,
) -> ReleasePromotion:
    promotion = store.get_release_promotion(version)
    if not promotion:
        raise ValueError(f"unknown release promotion: {version}")
    decide_transition(promotion.state, to_state)
    return store.transition_release_promotion(
        version,
        frozenset({promotion.state}),
        to_state,
        actor=actor,
        action=action,
        note=note,
        fields=fields,
    )


def _heartbeat_failure_reason(store, release, promotion, body, received_at: str) -> str:
    rollout = store.get_rollout(promotion.dev_rollout_id) if promotion.dev_rollout_id else None
    if not rollout or rollout.status != "success" or rollout.exec_status != "succeeded":
        return ""
    try:
        received = datetime.fromisoformat(received_at)
        completed = datetime.fromisoformat(rollout.completed_at)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        if received < completed:
            return ""
    except (TypeError, ValueError):
        return "dev_heartbeat_time_invalid"
    if not body.healthy:
        return "dev_heartbeat_unhealthy"
    if body.onebrain.version != release.version:
        return "dev_version_mismatch"
    if release.migration_to and body.onebrain.migration_revision != release.migration_to:
        return "dev_migration_mismatch"
    update = getattr(body, "update", None)
    if not update or update.attempt_id != rollout.id or update.outcome != "succeeded":
        return "dev_attempt_mismatch"
    if update.last_target_version != release.version:
        return "dev_target_mismatch"
    reported_modules = {report.module_id: report.version for report in body.modules}
    installed = {module.module_id for module in store.list_modules(rollout.deployment_id) if module.status == "active"}
    for module_id in sorted(installed):
        expected = release.modules.get(module_id)
        if not expected:
            return "dev_module_missing"
        actual = body.onebrain.version if module_id == "onebrain-api" else reported_modules.get(module_id, "")
        if actual != expected:
            return "dev_module_mismatch"
    return "verified"


def reconcile_heartbeat_promotion(store, body, *, received_at: str) -> Optional[ReleasePromotion]:
    """Apply trustworthy heartbeat facts to promotion state. Repeated calls are no-ops."""
    deployment = store.get_deployment(body.deployment_id)
    if not deployment:
        return None
    store.update_deployment_telemetry(
        body.deployment_id,
        heartbeat_at=received_at,
        healthy=body.healthy,
        reported_version=body.onebrain.version,
        reported_migration=body.onebrain.migration_revision,
    )
    deployment = store.get_deployment(body.deployment_id)
    if deployment.is_release_gate:
        candidates = [
            promotion for promotion in store.list_release_promotions()
            if promotion.state == "dev_deploying" and promotion.gate_deployment_id == deployment.id
        ]
        if not candidates:
            return None
        promotion = candidates[0]
        release = store.get_release(promotion.release_version)
        if not release:
            return None
        result = _heartbeat_failure_reason(store, release, promotion, body, received_at)
        if not result:
            return promotion
        if result == "verified":
            return transition(
                store,
                release.version,
                to_state="dev_verified",
                actor=f"fleet:{deployment.id}",
                action="dev_verified",
                fields={
                    "dev_completed_at": received_at,
                    "dev_verified_at": received_at,
                    "failure_reason": "",
                },
            )
        return transition(
            store,
            release.version,
            to_state="dev_failed",
            actor=f"fleet:{deployment.id}",
            action="dev_verification_failed",
            note=result,
            fields={"dev_completed_at": received_at, "failure_reason": result},
        )

    update = getattr(body, "update", None)
    if update and update.last_target_version:
        promotion = store.get_release_promotion(update.last_target_version)
        rollout = store.get_rollout(update.attempt_id) if update.attempt_id else None
        exact_attempt = bool(
            rollout
            and rollout.deployment_id == deployment.id
            and rollout.target_version == update.last_target_version
        )
        failure_reason = ""
        if exact_attempt and update.outcome in {"failed", "rolled_back"}:
            failure_reason = "customer_update_failed"
        elif not body.healthy and exact_attempt:
            failure_reason = "customer_health_failed"
        if promotion and promotion.state == "customer_approved" and failure_reason:
            return transition(
                store,
                update.last_target_version,
                to_state="customer_paused",
                actor=f"fleet:{deployment.id}",
                action="customer_delivery_paused",
                note="authenticated customer update failure",
                fields={
                    "customer_paused_at": received_at,
                    "customer_paused_reason": failure_reason,
                    "failure_reason": failure_reason,
                },
            )
    return None


def reconcile_promotion_timeouts(
    store,
    *,
    now: datetime,
    deadline_seconds: int,
) -> list[ReleasePromotion]:
    """Fail dev attempts that outlive rollout or post-success verification deadlines."""
    changed: list[ReleasePromotion] = []
    for promotion in store.list_release_promotions():
        if promotion.state != "dev_deploying" or not promotion.dev_rollout_id:
            continue
        rollout = store.get_rollout(promotion.dev_rollout_id)
        if not rollout:
            continue
        if rollout.status == "failed":
            reconciled = reconcile_rollout_promotion(store, rollout)
            if reconciled:
                changed.append(reconciled)
            continue
        anchor = rollout.completed_at if rollout.status == "success" else (
            rollout.dispatched_at or promotion.dev_started_at
        )
        try:
            started = datetime.fromisoformat(anchor)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        clock = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if (clock - started).total_seconds() <= max(0, deadline_seconds):
            continue
        reason = "dev_verification_timeout" if rollout.status == "success" else "dev_convergence_timeout"
        try:
            changed.append(transition(
                store,
                promotion.release_version,
                to_state="dev_failed",
                actor="mission-control",
                action=reason,
                note=reason,
                fields={
                    "dev_completed_at": clock.isoformat(),
                    "failure_reason": reason,
                },
            ))
        except ValueError:
            # A heartbeat/callback won the compare-and-set race. Its terminal
            # state is authoritative; a timeout tick must never revive or replace it.
            continue
    return changed


def reconcile_rollout_promotion(store, rollout) -> Optional[ReleasePromotion]:
    """Freeze promotion state after an authenticated terminal rollout result."""
    promotion = store.get_release_promotion(rollout.target_version)
    deployment = store.get_deployment(rollout.deployment_id)
    if not promotion or not deployment:
        return None
    if deployment.is_release_gate and promotion.dev_rollout_id == rollout.id:
        if rollout.status == "failed" and promotion.state == "dev_deploying":
            return transition(
                store,
                rollout.target_version,
                to_state="dev_failed",
                actor="rollout-callback",
                action="dev_rollout_failed",
                note="development rollout failed",
                fields={
                    "dev_completed_at": rollout.completed_at or _now(),
                    "failure_reason": "dev_rollout_failed",
                },
            )
        return promotion
    if rollout.status == "failed" and promotion.state == "customer_approved":
        now = rollout.completed_at or _now()
        return transition(
            store,
            rollout.target_version,
            to_state="customer_paused",
            actor="rollout-callback",
            action="customer_delivery_paused",
            note="authenticated customer rollout failure",
            fields={
                "customer_paused_at": now,
                "customer_paused_reason": "customer_rollout_failed",
                "failure_reason": "customer_rollout_failed",
            },
        )
    return promotion


def attach_production_signature(
    store,
    version: str,
    *,
    signature: str,
    signing_key_id: str,
    production_public_key: str,
    actor: str = "operator",
) -> ReleaseManifest:
    """Verify a signature without activating the release or changing promotion state."""
    release = store.get_release(version)
    promotion = store.get_release_promotion(version)
    if not release or not promotion:
        raise ValueError(f"unknown release candidate: {version}")
    if promotion.state != "dev_verified":
        raise ValueError("release_not_dev_verified")
    verify_production_signature_match(
        release,
        signature=signature,
        production_public_key=production_public_key,
    )
    return store.set_release_production_signature(
        version,
        signature=signature,
        signing_key_id=signing_key_id.strip(),
        actor=actor,
    )

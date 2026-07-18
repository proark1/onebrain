"""Hetzner pull-rollout helpers.

Deployment targets are persisted in legacy-named columns for database
compatibility, but the only supported execution transport is the signed Hetzner
pull path.  There is deliberately no workflow dispatcher or callback executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict


HETZNER_TARGET_PREFIX = "hetzner:"
DEVELOPMENT_GATE_MODULE_IDS = frozenset({
    "onebrain-api",
    "onebrain-admin-ui",
    "onebrain-workers",
    "assistant-service",
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
})
SECRETS_EPOCH_PENDING_REASON = "development gate has not applied the expected secrets epoch"


@dataclass(frozen=True)
class PullTargetEligibility:
    allowed: bool
    provider: str = "unknown"
    source: str = ""
    reason: str = ""
    target: Dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_provisioned_target(prov_store, deployment_id: str) -> Dict:
    """Return the opaque Hetzner target from the latest successful provision.

    The database's legacy columns are translated at this boundary. A value
    without the Hetzner prefix is unknown and cannot be updated.
    """
    runs = [
        run
        for run in prov_store.list_runs(deployment_id=deployment_id)
        if run.status == "succeeded" and run.railway_project_id
    ]
    if not runs:
        raise ValueError(f"no successful Hetzner provisioning target for {deployment_id}")
    latest = sorted(runs, key=lambda run: (run.completed_at or run.created_at or "", run.id))[-1]
    return {
        "target_id": latest.railway_project_id,
        "target_environment": latest.railway_environment_id,
        "service_ids": (latest.result_payload or {}).get("service_ids", {}),
    }


def target_provider(target: Dict) -> str:
    """Classify persisted coordinates without reviving a legacy provider path."""
    project_id = str(target.get("target_id", ""))
    return "hetzner" if project_id.startswith(HETZNER_TARGET_PREFIX) else "unknown"


def resolve_pull_target(
    prov_store,
    control_store,
    fleet_store,
    deployment_id: str,
    *,
    heartbeat_max_age_seconds: int = 600,
    now: datetime | None = None,
) -> PullTargetEligibility:
    """Resolve a signed pull target without inventing provisioning history.

    A real successful Hetzner provisioning run remains the normal path. The only
    alternative is the currently designated development gate, whose deployment
    identity is proven by an active fleet key plus a fresh authenticated heartbeat.
    """
    missing_reason = f"no successful Hetzner provisioning target for {deployment_id}"
    try:
        target = resolve_provisioned_target(prov_store, deployment_id)
    except ValueError:
        target = {}
    if target_provider(target) == "hetzner":
        return PullTargetEligibility(
            allowed=True,
            provider="hetzner",
            source="provisioning_run",
            target=target,
        )

    fallback_reason = (
        "Rollout target is not a Hetzner deployment." if target else missing_reason
    )
    deployment = control_store.get_deployment(deployment_id)
    gate = control_store.get_release_gate()
    if not deployment or not gate or gate.id != deployment.id or not deployment.is_release_gate:
        return PullTargetEligibility(False, reason=fallback_reason)
    if deployment.environment != "development" or deployment.deployment_type != "dedicated_server":
        return PullTargetEligibility(False, reason="development gate shape is invalid")

    keys = [
        key for key in fleet_store.list_keys(deployment_id)
        if key.status == "active" and key.deployment_id == deployment_id
    ]
    if not keys:
        return PullTargetEligibility(False, reason="development gate has no active fleet key")

    installed_modules = {
        module.module_id
        for module in control_store.list_modules(deployment_id)
        if module.status == "active"
    }
    if installed_modules != DEVELOPMENT_GATE_MODULE_IDS:
        return PullTargetEligibility(False, reason="development gate module set is incomplete")

    heartbeat = fleet_store.latest_heartbeat(deployment_id)
    if heartbeat is None:
        return PullTargetEligibility(False, reason="development gate has no authenticated heartbeat")
    if heartbeat.deployment_id != deployment_id:
        return PullTargetEligibility(False, reason="development gate heartbeat identity mismatch")
    if heartbeat.healthy is not True:
        return PullTargetEligibility(False, reason="development gate heartbeat is unhealthy")
    try:
        received_at = datetime.fromisoformat(heartbeat.received_at)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return PullTargetEligibility(False, reason="development gate heartbeat timestamp is invalid")
    clock = now or datetime.now(timezone.utc)
    if (clock - received_at).total_seconds() > max(1, heartbeat_max_age_seconds):
        return PullTargetEligibility(False, reason="development gate heartbeat is stale")

    # Heartbeat ingest stamps the authenticated key's last_used_at with the same
    # server timestamp as the heartbeat. Retain a small legacy tolerance for rows
    # written before those timestamps were unified.
    key_proved_heartbeat = False
    for key in keys:
        try:
            used_at = datetime.fromisoformat(key.last_used_at)
            if used_at.tzinfo is None:
                used_at = used_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if abs((received_at - used_at).total_seconds()) <= 5:
            key_proved_heartbeat = True
            break
    if not key_proved_heartbeat:
        return PullTargetEligibility(
            False,
            reason="development gate heartbeat was not sent with an active fleet key",
        )

    get_bundle = getattr(prov_store, "get_secret_bundle", None)
    bundle = get_bundle(deployment_id) if callable(get_bundle) else None
    if bundle is None:
        return PullTargetEligibility(False, reason="development gate has no encrypted secret bundle")
    update = heartbeat.payload.get("update", {}) if isinstance(heartbeat.payload, dict) else {}
    try:
        applied_epoch = int(update.get("applied_secrets_epoch", 0) or 0)
    except (TypeError, ValueError):
        applied_epoch = 0
    if applied_epoch < int(bundle.secrets_epoch or 0):
        return PullTargetEligibility(False, reason=SECRETS_EPOCH_PENDING_REASON)

    return PullTargetEligibility(
        allowed=True,
        provider="hetzner",
        source="enrolled_development_gate",
    )


def mark_rollout_dispatch_failed(store, rollout, reason: str):
    """Terminal-fail an offer that cannot be sent through the Hetzner pull path."""
    store.update_rollout_status(rollout.id, "failed", notes=reason[:200])
    return store.update_rollout_exec(
        rollout.id,
        exec_status="dispatch_failed",
        failure_reason=reason[:1000],
        completed_at=_now(),
    )

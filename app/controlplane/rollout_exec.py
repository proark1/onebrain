"""Hetzner pull-rollout helpers.

Deployment targets are persisted in legacy-named columns for database
compatibility, but the only supported execution transport is the signed Hetzner
pull path.  There is deliberately no workflow dispatcher or callback executor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict


HETZNER_TARGET_PREFIX = "hetzner:"


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


def mark_rollout_dispatch_failed(store, rollout, reason: str):
    """Terminal-fail an offer that cannot be sent through the Hetzner pull path."""
    store.update_rollout_status(rollout.id, "failed", notes=reason[:200])
    return store.update_rollout_exec(
        rollout.id,
        exec_status="dispatch_failed",
        failure_reason=reason[:1000],
        completed_at=_now(),
    )

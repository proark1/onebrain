"""Rollout execution — the pure logic that turns a rollout row into a real
dispatched update and drives it to completion via callbacks.

Mirrors the provisioning executor (app/provisioning/runs.py): a monotonic status
rank, terminal immutability, and an injectable dispatch. The bookkeeping status
machine (ControlPlaneStore.update_rollout_status) is REUSED unchanged — it already
re-runs plan_update and atomically applies the release's module/version on success,
so the irreversible "apply the update" step keeps its existing safety gate. This
module only adds the execution lifecycle (dispatch -> running -> succeeded/failed)
and bridges it onto that machine.

Everything here is pure of network: build_rollout_dispatch_inputs and
apply_rollout_callback are unit-testable against the in-memory control store, and
the actual GitHub/Railway work is the dispatcher + workflow (the infra tail).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

# Execution lifecycle statuses (distinct from the coarse bookkeeping RUN_STATUSES).
ROLLOUT_EXEC_STATUSES = frozenset(
    {"pending", "dispatch_failed", "dispatched", "running", "succeeded", "failed", "cancelled"}
)
TERMINAL_EXEC_STATUSES = frozenset({"succeeded", "failed", "dispatch_failed", "cancelled"})
# Monotonic rank: a callback may only move forward. (copied shape from provisioning)
EXEC_STATUS_RANK = {
    "pending": 0, "dispatch_failed": 1, "dispatched": 2, "running": 3,
    "failed": 4, "cancelled": 4, "succeeded": 5,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_exec_status(status: str) -> None:
    if status not in ROLLOUT_EXEC_STATUSES:
        raise ValueError(f"unknown rollout exec status: {status}")


@dataclass(frozen=True)
class RolloutCallback:
    status: str
    external_run_id: str = ""
    external_run_url: str = ""
    migration_revision: str = ""
    smoke_status: str = ""
    failure_reason: str = ""
    result_payload: Dict = field(default_factory=dict)
    # A dry run exercises the dispatch/callback plumbing with NO Railway changes,
    # so its "succeeded" must NOT apply the release to the control plane (that
    # would record the deployment as upgraded while its infra is untouched, and
    # poison the next real rollout via plan_update's already_current).
    dry_run: bool = False


def build_rollout_dispatch_inputs(
    *,
    rollout,
    plan,
    release,
    deployment,
    railway: Dict,
    callback_url: str,
    callback_key_id: str,
    dry_run: bool,
) -> Dict[str, str]:
    """Pure builder for the update-customer workflow inputs. Only structural,
    workflow-inert values (versions, ids, JSON blobs) — no free text."""
    return {
        "rollout_id": rollout.id,
        "account_id": deployment.account_id,
        "deployment_id": deployment.id,
        "target_version": release.version,
        "git_sha": release.git_sha,
        "modules_to_update_json": json.dumps(plan.modules_to_update, sort_keys=True),
        "migration_from": release.migration_from,
        "migration_to": release.migration_to,
        "railway_project_id": railway.get("railway_project_id", ""),
        "railway_environment_id": railway.get("railway_environment_id", ""),
        "service_ids_json": json.dumps(railway.get("service_ids", {}), sort_keys=True),
        "callback_url": callback_url.replace("{rollout_id}", rollout.id),
        "callback_key_id": callback_key_id,
        "dry_run": "true" if dry_run else "false",
    }


def resolve_railway_target(prov_store, deployment_id: str) -> Dict:
    """The Railway coordinates to act against, read from the deployment's latest
    SUCCEEDED provisioning run. Fail-closed: a deployment we have never
    successfully provisioned has no known target and cannot be updated.

    D-6 slot convention (deliberate overload, decided Phase 3,
    docs/hetzner-migration-sequence.md): the Hetzner provisioner (P1) writes
    into these SAME columns — railway_project_id = "hetzner:<hetzner_server_id>"
    (string numeric id from the Hetzner Cloud API), railway_environment_id =
    "<compose_project_name>" (default onebrain-<deployment_id>), and
    result_payload["service_ids"] = {module_id: compose_service_name} where
    compose service names equal module ids. This resolver stays byte-identical;
    readers MUST classify the resolved coordinates via target_provider() and
    must never "fix" the column names."""
    runs = [
        r for r in prov_store.list_runs(deployment_id=deployment_id)
        if r.status == "succeeded" and r.railway_project_id
    ]
    if not runs:
        raise ValueError(f"no successful provisioning run with Railway coordinates for {deployment_id}")
    latest = sorted(runs, key=lambda r: (r.completed_at or r.created_at or "", r.id))[-1]
    return {
        "railway_project_id": latest.railway_project_id,
        "railway_environment_id": latest.railway_environment_id,
        "service_ids": (latest.result_payload or {}).get("service_ids", {}),
    }


# Deliberate slot overload (decided Phase 3, docs/hetzner-migration-sequence.md):
# the Hetzner provisioner (P1) writes railway_project_id = "hetzner:<server_id>",
# railway_environment_id = "<compose_project_name>", and
# result_payload.service_ids = {module_id: compose_service_name}. Readers MUST
# classify via target_provider() and must never "fix" the column names.
HETZNER_TARGET_PREFIX = "hetzner:"


def target_provider(railway: Dict) -> str:
    """'hetzner' when the resolved coordinates carry the hetzner: prefix, else
    'railway'. Pure; used by every dispatch site to fail closed on targets the
    GitHub/Railway executor cannot act on (the Hetzner path is pull-based, P2/P3)."""
    project_id = str(railway.get("railway_project_id", ""))
    return "hetzner" if project_id.startswith(HETZNER_TARGET_PREFIX) else "railway"


def mark_rollout_dispatch_failed(store, rollout, reason: str):
    """Dispatch never left the ground — terminal-fail both the bookkeeping status
    and the execution status."""
    store.update_rollout_status(rollout.id, "failed", notes=reason[:200])
    return store.update_rollout_exec(
        rollout.id, exec_status="dispatch_failed", failure_reason=reason[:1000], completed_at=_now()
    )


def apply_rollout_callback(store, rollout_id: str, callback: RolloutCallback):
    """Drive a rollout's execution lifecycle from a workflow callback.

    Order matters: the bookkeeping transition runs FIRST (its `success` path
    re-runs plan_update and applies versions irreversibly). If it refuses, the
    callback is rejected and exec_status is NOT advanced."""
    _validate_exec_status(callback.status)
    rollout = store.get_rollout(rollout_id)
    if not rollout:
        raise KeyError(f"unknown rollout: {rollout_id}")
    if rollout.exec_status in TERMINAL_EXEC_STATUSES:
        raise ValueError("terminal rollout cannot be modified")
    if EXEC_STATUS_RANK[callback.status] < EXEC_STATUS_RANK[rollout.exec_status]:
        raise ValueError("stale rollout callback cannot move status backward")

    if callback.status in ("dispatched", "running"):
        store.update_rollout_status(rollout_id, "running")
    elif callback.status == "succeeded":
        # Reach a clean terminal bookkeeping status, but apply the release (bump
        # module + deployment versions) ONLY for a real run. A dry run marks the
        # rollout success-verified without mutating the deployment's version.
        store.update_rollout_status(
            rollout_id, "success", notes=callback.smoke_status or "", apply=not callback.dry_run
        )
    else:  # failed | dispatch_failed | cancelled
        store.update_rollout_status(rollout_id, "failed", notes=(callback.failure_reason or "")[:200])

    exec_fields = {
        "exec_status": callback.status,
        "external_run_id": callback.external_run_id or rollout.external_run_id,
        "external_run_url": callback.external_run_url or rollout.external_run_url,
    }
    if callback.status in TERMINAL_EXEC_STATUSES:
        exec_fields["completed_at"] = _now()
    if callback.status in ("failed", "dispatch_failed", "cancelled"):
        exec_fields["failure_reason"] = (callback.failure_reason or "")[:1000]
    return store.update_rollout_exec(rollout_id, **exec_fields)

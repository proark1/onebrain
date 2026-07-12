"""Pull-path reconcile (architecture §3b/§3f, P2).

A Hetzner/pull box converges on its OWN signed desired-state (P4-05) and reports the
outcome in its fleet.v2 UpdateReport; Mission Control never dispatches a workflow to
it. This module turns those box-authored reports into child-rollout terminal statuses
and feeds the UNCHANGED advance_fleet_rollout via the UNCHANGED reconcile_fleet_rollout
— so a pull rollout rides the exact same ring-by-ring reducer as a Railway rollout.

PURE of network: the control/fleet stores, the latest-heartbeats snapshot, and the
clock are injected. No scheduler in P4 (a manual operator endpoint drives it, exactly
as run_watchdog stayed test-only); the daemon/cron is the Phase-5 infra tail. At rest
the tick is a no-op (no running fleet rollouts).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import ValidationError

from app.controlplane.base import BackupRun
from app.controlplane.fleet_runner import reconcile_fleet_rollout
from app.fleet.heartbeat import UpdateReport

_TERMINAL_ROLLOUT = frozenset({"success", "failed"})

# 7d/A17: the well-formed backup-manifest grammar — "sha256:<64 lowercase hex>:<bytes>".
# The box records this (digest + size of the ENCRYPTED backup object) after a successful
# encrypt; MC treats a "success" WITHOUT a well-formed manifest as no backup at all.
_BACKUP_MANIFEST_RE = re.compile(r"^sha256:[0-9a-f]{64}:\d+$")


def parse_backup_manifest(manifest: str) -> Optional[str]:
    """Return `manifest` iff it is a well-formed 'sha256:<64hex>:<bytes>' string, else
    None. A bare backup_status=='success' with no/garbled manifest resolves to None
    (treated as NO backup) — a phantom-backup box cannot disable its own migration-
    crossing restore net by asserting a naked success (7d/A17)."""
    return manifest if _BACKUP_MANIFEST_RE.match(manifest or "") else None


def _parse_dispatched_at(dispatched_at: str) -> Optional[datetime]:
    if not dispatched_at:
        return None
    try:
        parsed = datetime.fromisoformat(dispatched_at)
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _past_deadline(child, now: datetime, deadline_seconds: int) -> bool:
    """True iff now is strictly past dispatched_at + deadline. A missing/garbled
    dispatched_at means 'no deadline yet' (False) — NEVER an immediate failure, so a
    freshly-offered child with an unparseable stamp keeps waiting rather than being
    synthesized failed."""
    dispatched = _parse_dispatched_at(getattr(child, "dispatched_at", "") or "")
    if dispatched is None:
        return False
    return (now - dispatched).total_seconds() > deadline_seconds


def synthesize_pull_status(child, update_report, *, now: datetime, deadline_seconds: int) -> Optional[str]:
    """Pure. Returns 'success' | 'failed' | None(keep waiting) for one OFFERED pull child.

    - attempt_id != child.id  -> the box has not acted on THIS offer yet:
          past deadline (now - dispatched_at > deadline) -> 'failed' (silent box); else None.
    - attempt_id == child.id:
          outcome 'succeeded'                 -> 'success'
          outcome 'failed' | 'rolled_back'    -> 'failed'
          outcome 'none' | 'in_progress'      -> None, unless past deadline -> 'failed'.

    dispatched_at is parsed defensively; a missing/garbled value is 'no deadline yet'."""
    overdue = _past_deadline(child, now, deadline_seconds)
    if update_report.attempt_id != child.id:
        return "failed" if overdue else None
    outcome = update_report.outcome
    if outcome == "succeeded":
        return "success"
    if outcome in ("failed", "rolled_back"):
        return "failed"
    return "failed" if overdue else None   # none | in_progress before deadline -> keep waiting


def materialize_backup_from_report(control_store, deployment_id: str, update_report) -> None:
    """C3: a pull box holds only a heartbeat-scoped fleet key and cannot call
    record_backup, yet the plan gate requires a fresh BackupRun before a
    migration-crossing rollout. When the box's UpdateReport claims
    backup_status=='success', materialize a BackupRun so the NEXT migration-crossing
    rollout to it is not permanently backup-blocked. Idempotent: only a fresh
    backup_ts records a new row (a deterministic id + the latest-backup guard both
    absorb re-ticks of the same heartbeat).

    A17 — RESIDUAL TRUST (7d, Phase 5 mitigation). MC now GATES this on a WELL-FORMED
    backup_manifest ("sha256:<64hex>:<bytes>", the digest+size of the encrypted backup
    object the box records after a successful encrypt): a "success" carrying no/garbled
    manifest is treated as NO backup, so a phantom-backup box can no longer disable its
    own restore_required net by asserting a bare success. This raises the bar from "any
    empty assertion" to "a well-formed manifest" and pairs with the pre-migration alembic
    revision the box already records. RESIDUAL (stated, not silently carried): the box
    still AUTHORS the hash, so a fully-compromised box can fabricate a well-formed one;
    full closure — an off-box confirmation of the backup object via MC's OWN storage read
    — remains a §6 ops item, not this field. The manifest is recorded in BackupRun.detail
    so the operator can cross-check it."""
    if update_report.backup_status != "success" or not update_report.backup_ts:
        return
    manifest = parse_backup_manifest(getattr(update_report, "backup_manifest", "") or "")
    if manifest is None:
        return  # 7d/A17 gate: a bare/garbled "success" is not a backup — do not net it
    detail = f"pull-report:{update_report.backup_ts}:{manifest}"
    latest = control_store.latest_backup(deployment_id)
    if latest is not None and latest.detail == detail and latest.status == "success":
        return  # already materialized this backup_ts (fast path for the common re-tick)
    try:
        control_store.record_backup(BackupRun(
            id=f"bkp_pull_{deployment_id}_{update_report.backup_ts}",
            deployment_id=deployment_id, status="success", detail=detail,
            created_at=update_report.backup_ts,
        ))
    except ValueError:
        return  # unknown deployment, or this backup_ts already recorded — idempotent


def _report_from_heartbeat(heartbeat) -> UpdateReport:
    """Extract the UpdateReport from a stored heartbeat (payload['update']). Defaults to
    an empty UpdateReport (outcome 'none') for a v1 box / no heartbeat / malformed
    update — a box that has never reported for THIS offer keeps waiting until its
    deadline, never synthesized failed on the strength of an absent report alone."""
    payload = getattr(heartbeat, "payload", None) or {}
    update = payload.get("update") or {}
    try:
        return UpdateReport.model_validate(update)
    except ValidationError:
        return UpdateReport()


def _apply_child_status(control_store, child, status: str, update_report, *, now: datetime) -> None:
    """Drive the child to its synthesized terminal status. A 'success' goes through the
    UNCHANGED update_rollout_status apply path — the SAME plan_update gate the Railway
    callback uses (apply_rollout_callback) — so a pull success applies the release
    irreversibly under the identical safety gate, never a parallel apply path."""
    if status == "success":
        control_store.update_rollout_status(child.id, "success")
        control_store.update_rollout_exec(child.id, exec_status="succeeded", completed_at=now.isoformat())
        return
    reported_failure = (update_report.attempt_id == child.id
                        and update_report.outcome in ("failed", "rolled_back"))
    reason = "pull_reported_failure" if reported_failure else "pull_convergence_timeout"
    control_store.update_rollout_status(child.id, "failed", notes=reason)
    control_store.update_rollout_exec(child.id, exec_status="failed", completed_at=now.isoformat(),
                                      failure_reason=reason)


def reconcile_pull_targets(control_store, fleet_store, latest_heartbeats: dict, *, now: datetime,
                           deadline_seconds: int, dispatch_child) -> List:
    """The tick. For each RUNNING fleet rollout, for each of its NON-TERMINAL children
    whose request_payload marks it a hetzner/pull target:
      1. read the deployment's latest heartbeat -> payload['update'] -> UpdateReport.
      2. materialize_backup_from_report(...).
      3. status = synthesize_pull_status(...); None -> keep waiting (skip).
      4. drive the child to that terminal status (success applies via the existing gate).
      5. feed the UNCHANGED reconcile_fleet_rollout -> UNCHANGED advance_fleet_rollout.
    Returns the reconciled fleet runs. Railway children are left untouched (their
    workflow callback owns them). Pure of network; heartbeats + clock injected."""
    runs = []
    for fleet_run in fleet_store.list_fleet_rollouts():
        if fleet_run.status != "running":
            continue
        for child in control_store.list_rollouts_for_fleet(fleet_run.id):
            if child.status in _TERMINAL_ROLLOUT:
                continue
            if not (child.request_payload or {}).get("pull"):
                continue  # a Railway child — the workflow callback path owns it
            report = _report_from_heartbeat(latest_heartbeats.get(child.deployment_id))
            materialize_backup_from_report(control_store, child.deployment_id, report)
            status = synthesize_pull_status(child, report, now=now, deadline_seconds=deadline_seconds)
            if status is None:
                continue
            _apply_child_status(control_store, child, status, report, now=now)
        run = reconcile_fleet_rollout(control_store, fleet_store, fleet_run.id, dispatch_child=dispatch_child)
        if run is not None:
            runs.append(run)
    return runs

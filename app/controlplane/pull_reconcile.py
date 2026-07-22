"""Pull-path reconcile (architecture §3b/§3f, P2).

A Hetzner/pull box converges on its OWN signed desired-state (P4-05) and reports the
outcome in its fleet.v2 UpdateReport; Mission Control never dispatches a workflow to
it. This module turns those box-authored reports into child-rollout terminal statuses
and feeds the UNCHANGED advance_fleet_rollout via the UNCHANGED reconcile_fleet_rollout
— so a pull rollout rides the exact same ring-by-ring reducer as a workflow-backed rollout.

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
from app.controlplane.development_gate import (
    validate_module_transition,
    verify_reported_modules,
)
from app.controlplane.fleet_runner import reconcile_fleet_rollout
from app.controlplane.promotion import reconcile_promotion_timeouts, reconcile_rollout_promotion
from app.fleet.heartbeat import FleetHeartbeatV2, UpdateReport

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


def _heartbeat_from_stored(heartbeat) -> Optional[FleetHeartbeatV2]:
    """Return a fully validated v2 heartbeat or None.

    Pull acknowledgement is a release-attestation boundary, not merely an
    update-state parser.  A valid ``UpdateReport`` nested in an otherwise
    malformed or v1 heartbeat is therefore insufficient to complete a rollout.
    """
    payload = getattr(heartbeat, "payload", None) or {}
    try:
        return FleetHeartbeatV2.model_validate(payload)
    except ValidationError:
        return None


def pull_acknowledgement_matches(control_store, child, heartbeat) -> bool:
    """True only for the exact healthy, fully converged pull acknowledgement.

    The box's outcome is advisory until every independently observable fact
    agrees with the rollout it was offered.  Any mismatch remains non-terminal
    and is converted into a timeout only by ``synthesize_pull_status``.
    """
    body = _heartbeat_from_stored(heartbeat)
    release = control_store.get_release(child.target_version)
    deployment = control_store.get_deployment(child.deployment_id)
    if not body or not release or not deployment:
        return False
    if body.deployment_id != child.deployment_id or not body.healthy:
        return False

    update = body.update
    if update.attempt_id != child.id or update.outcome != "succeeded":
        return False
    if update.last_target_version != release.version or body.onebrain.version != release.version:
        return False

    # A release without a schema transition must still attest the deployment's
    # current revision.  This prevents an empty migration_to from weakening the
    # acknowledgement check.
    expected_migration = release.migration_to or deployment.current_migration
    if expected_migration and (
        update.migration_reached != expected_migration
        or body.onebrain.migration_revision != expected_migration
    ):
        return False

    if deployment.is_release_gate:
        current_module_ids = {
            module.module_id
            for module in control_store.list_modules(child.deployment_id)
            if module.status == "active"
        }
        if validate_module_transition(current_module_ids, release.modules):
            return False
        try:
            required_secrets_epoch = max(
                0,
                int((child.request_payload or {}).get("required_secrets_epoch", 0)),
            )
        except (TypeError, ValueError):
            return False
        if update.applied_secrets_epoch < required_secrets_epoch:
            return False
        _, reason = verify_reported_modules(body, release.modules)
        return not reason

    reported_modules = {}
    for report in body.modules:
        if report.module_id in reported_modules:
            return False
        reported_modules[report.module_id] = report
    for module in control_store.list_modules(child.deployment_id):
        if module.status != "active":
            continue
        expected_version = release.modules.get(module.module_id)
        report = reported_modules.get(module.module_id)
        if not expected_version or not report:
            return False
        if not report.healthy or report.version != expected_version:
            return False
    return True


def verified_development_gate_modules(control_store, child, heartbeat) -> Optional[dict[str, str]]:
    """Return exact authenticated module evidence only for a promotion-linked gate rollout."""
    deployment = control_store.get_deployment(child.deployment_id)
    promotion = control_store.get_release_promotion(child.target_version)
    if (
        not deployment
        or not deployment.is_release_gate
        or not promotion
        or promotion.state != "dev_deploying"
        or promotion.gate_deployment_id != deployment.id
        or promotion.dev_rollout_id != child.id
        or not pull_acknowledgement_matches(control_store, child, heartbeat)
    ):
        return None
    body = _heartbeat_from_stored(heartbeat)
    release = control_store.get_release(child.target_version)
    if not body or not release:
        return None
    verified, reason = verify_reported_modules(body, release.modules)
    return None if reason else verified


def synthesize_pull_status(
    child,
    update_report,
    *,
    now: datetime,
    deadline_seconds: int,
    success_verified: bool = False,
) -> Optional[str]:
    """Pure. Returns 'success' | 'failed' | None(keep waiting) for one OFFERED pull child.

    - attempt_id != child.id  -> the box has not acted on THIS offer yet:
          past deadline (now - dispatched_at > deadline) -> 'failed' (silent box); else None.
    - attempt_id == child.id:
          outcome 'succeeded' + verified facts -> 'success'
          outcome 'succeeded' + any mismatch  -> wait, then timeout-fail
          outcome 'failed' | 'rolled_back'    -> 'failed'
          outcome 'none' | 'in_progress'      -> None, unless past deadline -> 'failed'.

    dispatched_at is parsed defensively; a missing/garbled value is 'no deadline yet'."""
    overdue = _past_deadline(child, now, deadline_seconds)
    if update_report.attempt_id != child.id:
        return "failed" if overdue else None
    outcome = update_report.outcome
    if outcome == "succeeded":
        if success_verified:
            return "success"
        return "failed" if overdue else None
    if outcome in ("failed", "rolled_back"):
        return "failed"
    return "failed" if overdue else None   # none | in_progress before deadline -> keep waiting


def operator_self_converged(control_store, child, heartbeat) -> bool:
    """True when Mission Control's OWN box has converged on the offered release: a
    healthy self-heartbeat reporting the exact target version (and the target migration,
    when the release carries one). MC self-deploys the whole compose atomically, so
    box-level version + health IS the convergence signal — this path deliberately does
    NOT require the host updater's per-module UpdateReport (attempt_id/outcome/modules),
    because MC's in-app reporter authors version + health, not that host state."""
    body = _heartbeat_from_stored(heartbeat)
    release = control_store.get_release(child.target_version)
    deployment = control_store.get_deployment(child.deployment_id)
    if not body or not release or not deployment:
        return False
    if body.deployment_id != child.deployment_id or not body.healthy:
        return False
    if body.onebrain.version != release.version:
        return False
    expected_migration = release.migration_to or deployment.current_migration
    if expected_migration and body.onebrain.migration_revision != expected_migration:
        return False
    # If the heartbeat carries per-module health, every release module it reports must be
    # healthy AND on the release version — so a self-update where the API returns on the
    # target version but another MC service (e.g. onebrain-admin-ui) is still down does NOT
    # read as converged (MC's top-level `healthy` is API/data-store only). A box that
    # reports no module health falls back to version+health — a documented residual.
    reported = {report.module_id: report for report in (getattr(body, "modules", None) or [])}
    for module_id, expected_version in release.modules.items():
        report = reported.get(module_id)
        if report is not None and (not report.healthy or report.version != expected_version):
            return False
    return True


def synthesize_operator_self_status(child, *, converged: bool, now: datetime,
                                    deadline_seconds: int) -> Optional[str]:
    """Pure. 'success' once MC's box reports the target version healthy; 'failed' only
    after the convergence deadline; None while still converging. Unlike the gate/fleet
    pull path this does NOT consult the host updater's UpdateReport — version + health
    from MC's in-app heartbeat is the whole convergence signal for an atomic self-deploy
    (a missing/garbled dispatched_at is 'no deadline yet', never an immediate failure)."""
    if converged:
        return "success"
    return "failed" if _past_deadline(child, now, deadline_seconds) else None


def _reconcile_operator_self_pull(control_store, latest_heartbeats: dict, *, now: datetime,
                                  deadline_seconds: int, operator_self_deployment_id: str) -> None:
    """Converge the standalone pull rollout that moves Mission Control's OWN box to the
    development-verified tip (opened by dispatch_operator_self_rollout). Mirrors
    _reconcile_development_pull but keys on MC's deployment id and confirms success from
    version + health telemetry rather than the host updater's per-module report. Never
    raises: a self-update completion failure must not abort the fleet reconcile tick nor
    500 the manual endpoint that also drives it."""
    if not operator_self_deployment_id:
        return
    try:
        child = control_store.list_active_rollout(operator_self_deployment_id)
        if (not child or child.status in _TERMINAL_ROLLOUT or child.fleet_rollout_id
                or not (child.request_payload or {}).get("pull")):
            return
        heartbeat = latest_heartbeats.get(child.deployment_id)
        status = synthesize_operator_self_status(
            child,
            converged=operator_self_converged(control_store, child, heartbeat),
            now=now,
            deadline_seconds=deadline_seconds,
        )
        # Apply the terminal status DIRECTLY — deliberately NOT through _apply_child_status,
        # because that path calls reconcile_rollout_promotion. MC's self-update must NEVER
        # advance or pause a promotion: that state belongs to the dev gate (dev_verified)
        # and the operator (customer_approved). MC also self-rolls customer_approved
        # releases, and reconcile_rollout_promotion would flip a customer_approved release
        # to customer_paused on a FAILED rollout (promotion.py) — halting CUSTOMER delivery
        # because MC's own box hiccuped. Success still applies MC's own convergence
        # (current_version + module versions) via update_rollout_status.
        if status == "success":
            control_store.update_rollout_status(child.id, "success")
            control_store.update_rollout_exec(
                child.id, exec_status="succeeded", completed_at=now.isoformat())
        elif status == "failed":
            reason = "operator_self_convergence_timeout"
            control_store.update_rollout_status(child.id, "failed", notes=reason)
            control_store.update_rollout_exec(
                child.id, exec_status="failed", completed_at=now.isoformat(),
                failure_reason=reason)
    except Exception:  # pragma: no cover - defensive; the outer tick logs and continues
        return


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
    body = _heartbeat_from_stored(heartbeat)
    return body.update if body is not None else UpdateReport()


def _apply_child_status(
    control_store,
    child,
    status: str,
    update_report,
    *,
    now: datetime,
    verified_modules: Optional[dict[str, str]] = None,
) -> None:
    """Drive one child to its synthesized terminal status.

    Normal pull success retains the established apply path. A promotion-linked
    development-gate success atomically persists exact authenticated module
    evidence with deployment and rollout completion.
    """
    if status == "success":
        if verified_modules is not None:
            control_store.complete_verified_rollout(
                child.id,
                verified_modules=verified_modules,
                completed_at=now.isoformat(),
            )
        else:
            control_store.update_rollout_status(child.id, "success")
            control_store.update_rollout_exec(
                child.id,
                exec_status="succeeded",
                completed_at=now.isoformat(),
            )
        reconcile_rollout_promotion(control_store, control_store.get_rollout(child.id))
        return
    reported_failure = (update_report.attempt_id == child.id
                        and update_report.outcome in ("failed", "rolled_back"))
    reason = "pull_reported_failure" if reported_failure else "pull_convergence_timeout"
    control_store.update_rollout_status(child.id, "failed", notes=reason)
    control_store.update_rollout_exec(child.id, exec_status="failed", completed_at=now.isoformat(),
                                      failure_reason=reason)
    reconcile_rollout_promotion(control_store, control_store.get_rollout(child.id))


def _reconcile_development_pull(control_store, latest_heartbeats: dict, *, now: datetime,
                                deadline_seconds: int) -> None:
    """Converge the standalone pull rollout used by the development gate."""
    for promotion in control_store.list_release_promotions():
        if promotion.state != "dev_deploying" or not promotion.dev_rollout_id:
            continue
        child = control_store.get_rollout(promotion.dev_rollout_id)
        if (not child or child.status in _TERMINAL_ROLLOUT or child.fleet_rollout_id
                or not (child.request_payload or {}).get("pull")):
            continue
        heartbeat = latest_heartbeats.get(child.deployment_id)
        report = _report_from_heartbeat(heartbeat)
        materialize_backup_from_report(control_store, child.deployment_id, report)
        verified_modules = verified_development_gate_modules(
            control_store,
            child,
            heartbeat,
        )
        status = synthesize_pull_status(
            child,
            report,
            now=now,
            deadline_seconds=deadline_seconds,
            success_verified=verified_modules is not None,
        )
        if status is not None:
            _apply_child_status(
                control_store,
                child,
                status,
                report,
                now=now,
                verified_modules=verified_modules,
            )


def reconcile_pull_targets(control_store, fleet_store, latest_heartbeats: dict, *, now: datetime,
                           deadline_seconds: int, dispatch_child,
                           operator_self_deployment_id: str = "") -> List:
    """The tick. For each RUNNING fleet rollout, for each of its NON-TERMINAL children
    whose request_payload marks it a hetzner/pull target:
      1. read the deployment's latest heartbeat -> payload['update'] -> UpdateReport.
      2. materialize_backup_from_report(...).
      3. status = synthesize_pull_status(...); None -> keep waiting (skip).
      4. drive the child to that terminal status (success applies via the existing gate).
      5. feed the UNCHANGED reconcile_fleet_rollout -> UNCHANGED advance_fleet_rollout.
    Returns the reconciled fleet runs. Workflow-dispatched children are left untouched
    because their callback owns them. Pure of network; heartbeats + clock injected.

    operator_self_deployment_id (Mission Control only, when operator self-deploy is on)
    also converges MC's OWN standalone self-update rollout — a target that belongs to
    neither a fleet run nor a dev-gate promotion, so neither loop below would see it."""
    # Development candidates are standalone pull rollouts, not fleet children.
    # Consume their authenticated report before timeout evaluation so a success
    # already received at the deadline cannot be incorrectly failed first.
    _reconcile_development_pull(
        control_store, latest_heartbeats, now=now, deadline_seconds=deadline_seconds,
    )
    # Mission Control's own self-update rollout is likewise standalone (no fleet_rollout_id,
    # its promotion is dev_verified not dev_deploying), so converge it on the same tick.
    _reconcile_operator_self_pull(
        control_store, latest_heartbeats, now=now, deadline_seconds=deadline_seconds,
        operator_self_deployment_id=operator_self_deployment_id,
    )
    reconcile_promotion_timeouts(
        control_store,
        now=now,
        deadline_seconds=deadline_seconds,
    )
    runs = []
    for fleet_run in fleet_store.list_fleet_rollouts():
        if fleet_run.status != "running":
            continue
        for child in control_store.list_rollouts_for_fleet(fleet_run.id):
            if child.status in _TERMINAL_ROLLOUT:
                continue
            if not (child.request_payload or {}).get("pull"):
                continue  # a workflow child — the callback path owns it
            heartbeat = latest_heartbeats.get(child.deployment_id)
            report = _report_from_heartbeat(heartbeat)
            materialize_backup_from_report(control_store, child.deployment_id, report)
            status = synthesize_pull_status(
                child,
                report,
                now=now,
                deadline_seconds=deadline_seconds,
                success_verified=pull_acknowledgement_matches(control_store, child, heartbeat),
            )
            if status is None:
                continue
            _apply_child_status(control_store, child, status, report, now=now)
        run = reconcile_fleet_rollout(control_store, fleet_store, fleet_run.id, dispatch_child=dispatch_child)
        if run is not None:
            runs.append(run)
    return runs

"""Turn release-pipeline stalls into fleet alerts (roadmap Gap D — detection).

The heartbeat watchdog ([watchdog.py](watchdog.py)) turns per-deployment HEARTBEAT
state into alerts. This turns CONTROL-PLANE state — a development candidate stuck at
``dev_failed``, Mission Control's own self-deploy giving up, the designated release GATE
sitting hard-down (roadmap Phase 4 / Gap E2 Tier 1) — into the SAME fleet-alert ledger,
scoped to Mission Control's own deployment row so the signals surface in the fleet overview
next to infra alerts. The gate signal is detect-and-alert ONLY: it recommends a manual
replacement, never provisions or tears down anything.

Detection is pure (``desired_pipeline_alerts``); ``run_pipeline_watchdog`` reconciles it
and touches ONLY its own kinds, so it never disturbs the infra alerts on the same row.

Deliberately channel-agnostic: this makes the signals exist and be visible. PUSHING them
(email / webhook) is a separate later layer (roadmap Gap D fork #4) that reads the very
same ``FleetAlert`` rows.

The control store is duck-typed (``list_release_promotions`` / ``list_release_promotion_events``
/ ``list_rollouts`` / ``get_deployment`` / ``get_release_gate``) so this stays in ``app.fleet``
with no import back into the control-plane package.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.fleet.base import (
    DEV_PIPELINE_STALLED_ALERT,
    GATE_REPLACEMENT_RECOMMENDED_ALERT,
    OPERATOR_SELF_DEPLOY_STALLED_ALERT,
    FleetAlert,
)
from app.trust.envelope import compare_versions

# A migration-crossing candidate legitimately waits for the gate's daily 02:30 backup;
# that is a scheduled wait, not a stall, so it is excluded from the stall signal. Kept as
# a local literal so this module carries no dependency on the (separately shipped)
# development auto-retry module that also classifies this reason.
_BACKUP_WAIT_REASON = "backup_required_for_schema_update"
_PREFLIGHT_FAILURE_REASON = "dev_preflight_failed"

# The infra-alert kinds (opened on the gate's OWN row by the heartbeat watchdog) that mean the
# gate is HARD-down and the pipeline is stalled behind it. `unhealthy` is deliberately excluded:
# an app restart self-recovers, so it is too soft to recommend a (heavy) gate replacement on
# (design fork B). A dead box (missed_heartbeat) and disk death — low_root/low_data plus an
# unavailable data volume, the 2026-07-22 case — are the strong signals.
_GATE_FAILURE_ALERT_KINDS = frozenset({
    "missed_heartbeat", "low_root_disk", "low_data_disk", "data_volume_unavailable",
})

# Every kind this watchdog OWNS on Mission Control's row — the reciprocal of watchdog.py's
# WATCHDOG_ALERT_KINDS. run_pipeline_watchdog resolves only these, never the infra alerts.
_PIPELINE_ALERT_KINDS = frozenset({
    DEV_PIPELINE_STALLED_ALERT,
    OPERATOR_SELF_DEPLOY_STALLED_ALERT,
    GATE_REPLACEMENT_RECOMMENDED_ALERT,
})


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _newest_verified_version(promotions) -> str:
    best = ""
    for promotion in promotions:
        if getattr(promotion, "state", "") not in {"dev_verified", "customer_approved"}:
            continue
        version = promotion.release_version
        if not best or (compare_versions(version, best) or 0) > 0:
            best = version
    return best


def _is_superseded(version: str, newest_verified: str) -> bool:
    if not newest_verified:
        return False
    comparison = compare_versions(version, newest_verified)
    return comparison is not None and comparison <= 0


def _effective_reason(promotion, events) -> str:
    """The concrete failure reason: a generic ``dev_preflight_failed`` is unwrapped to the
    plan reason in the final event note (mirrors is_current_replacement_bootstrap_failure)."""
    reason = (getattr(promotion, "failure_reason", "") or "").strip()
    if reason == _PREFLIGHT_FAILURE_REASON and events:
        return (getattr(events[-1], "note", "") or "").strip() or reason
    return reason


def _failed_at(promotion) -> Optional[datetime]:
    return _parse_ts(
        getattr(promotion, "dev_completed_at", "") or getattr(promotion, "updated_at", "")
    )


def _sustained_gate_failure(
    fleet_store, gate_id: str, now: datetime, threshold: int
) -> Optional[tuple[str, float]]:
    """The gate's oldest open HARD-failure alert whose age exceeds ``threshold``, as
    ``(kind, age_seconds)``; ``None`` if the gate has no such sustained failure.

    Reuses the infra alerts the heartbeat watchdog already opened on the GATE's own row, so a
    transient blip the watchdog will resolve on the next healthy heartbeat never trips the
    recommendation. Only ``_GATE_FAILURE_ALERT_KINDS`` count (a soft ``unhealthy`` is ignored)."""
    worst: Optional[tuple[str, float]] = None
    for alert in fleet_store.list_open_alerts(gate_id):
        if alert.kind not in _GATE_FAILURE_ALERT_KINDS:
            continue
        created = _parse_ts(getattr(alert, "created_at", "") or "")
        if created is None:
            continue
        age = (now - created).total_seconds()
        if age <= threshold:
            continue
        if worst is None or age > worst[1]:
            worst = (alert.kind, age)
    return worst


def desired_pipeline_alerts(
    control_store,
    *,
    now: datetime,
    mc_deployment_id: str,
    stall_seconds: int,
    self_deploy_enabled: bool,
    self_max_attempts: int,
    fleet_store=None,
    gate_replace_seconds: int = 0,
) -> Dict[str, str]:
    """Which pipeline alerts SHOULD be open for Mission Control, kind -> detail.

    The gate-replacement signal reads the gate's infra alerts, so it needs ``fleet_store``; it
    is skipped when that (or ``gate_replace_seconds``) is absent, keeping the other two signals
    a pure function of control-plane state."""
    alerts: Dict[str, str] = {}
    promotions = list(control_store.list_release_promotions())
    newest_verified = _newest_verified_version(promotions)

    # 1. A development candidate stuck at dev_failed past the threshold — but not one
    #    superseded by a newer verified release, and not one merely waiting for the daily
    #    backup (a scheduled wait, not a stall).
    if stall_seconds > 0:
        stalled: List[tuple[str, float]] = []
        for promotion in promotions:
            if getattr(promotion, "state", "") != "dev_failed":
                continue
            version = promotion.release_version
            if _is_superseded(version, newest_verified):
                continue
            events = control_store.list_release_promotion_events(version)
            if _effective_reason(promotion, events) == _BACKUP_WAIT_REASON:
                continue
            failed_at = _failed_at(promotion)
            if failed_at is None:
                continue
            age = (now - failed_at).total_seconds()
            if age > stall_seconds:
                stalled.append((version, age))
        if stalled:
            stalled.sort(key=lambda item: item[1], reverse=True)
            version, age = stalled[0]
            extra = f"; {len(stalled)} candidates stalled" if len(stalled) > 1 else ""
            alerts[DEV_PIPELINE_STALLED_ALERT] = (
                f"release {version} stuck at dev_failed for {int(age)}s "
                f"(threshold {stall_seconds}s){extra}"
            )

    # 2. Mission Control's own self-deploy has given up on the newest verified release —
    #    only meaningful when auto-deploy is on (otherwise MC lagging is expected).
    if self_deploy_enabled and mc_deployment_id and newest_verified:
        deployment = control_store.get_deployment(mc_deployment_id)
        on_target = deployment is not None and deployment.current_version == newest_verified
        if not on_target:
            failures = [
                rollout for rollout in control_store.list_rollouts(mc_deployment_id)
                if rollout.target_version == newest_verified and rollout.status == "failed"
            ]
            if len(failures) >= max(1, int(self_max_attempts)):
                alerts[OPERATOR_SELF_DEPLOY_STALLED_ALERT] = (
                    f"Mission Control self-deploy of {newest_verified} failed "
                    f"{len(failures)} times (budget {self_max_attempts})"
                )

    # 3. The DESIGNATED release gate has a sustained hard-failure (dead box / disk death) —
    #    recommend provisioning a replacement, since a dead gate stalls the WHOLE dev pipeline
    #    (nothing new can dev_verify). Tier 1 = DETECT-AND-ALERT ONLY: the operator still runs
    #    the manual replace; nothing is auto-provisioned or torn down here.
    if gate_replace_seconds > 0 and fleet_store is not None:
        gate = control_store.get_release_gate()
        if gate is not None and gate.id and gate.id != mc_deployment_id:
            sustained = _sustained_gate_failure(fleet_store, gate.id, now, gate_replace_seconds)
            if sustained is not None:
                kind, age = sustained
                alerts[GATE_REPLACEMENT_RECOMMENDED_ALERT] = (
                    f"release gate {gate.id} has open {kind} for {int(age)}s "
                    f"(threshold {gate_replace_seconds}s); provision a replacement gate"
                )
    return alerts


def run_pipeline_watchdog(
    control_store,
    fleet_store,
    *,
    now_iso: str,
    mc_deployment_id: str,
    stall_seconds: int,
    self_deploy_enabled: bool,
    self_max_attempts: int,
    next_id,
    gate_replace_seconds: int = 0,
) -> List[FleetAlert]:
    """Open/resolve pipeline alerts on Mission Control's deployment row.

    Only ever touches the pipeline kinds (_PIPELINE_ALERT_KINDS) — it opens what is wanted and
    resolves any of ITS kinds no longer wanted, leaving the heartbeat watchdog's infra alerts
    on the same row untouched (the reciprocal of watchdog.py's WATCHDOG_ALERT_KINDS scoping)."""
    if not mc_deployment_id:
        return []
    now = _parse_ts(now_iso) or datetime.now(timezone.utc)
    want = desired_pipeline_alerts(
        control_store, now=now, mc_deployment_id=mc_deployment_id,
        stall_seconds=stall_seconds, self_deploy_enabled=self_deploy_enabled,
        self_max_attempts=self_max_attempts,
        fleet_store=fleet_store, gate_replace_seconds=gate_replace_seconds,
    )
    opened: List[FleetAlert] = []
    for kind, detail in want.items():
        if not fleet_store.has_open_alert(mc_deployment_id, kind):
            opened.append(fleet_store.open_alert(FleetAlert(
                id=next_id(), deployment_id=mc_deployment_id, kind=kind,
                detail=detail, status="open", created_at=now_iso,
            )))
    for existing in fleet_store.list_open_alerts(mc_deployment_id):
        if existing.kind in _PIPELINE_ALERT_KINDS and existing.kind not in want:
            fleet_store.resolve_open_alerts(mc_deployment_id, existing.kind, now_iso)
    return opened

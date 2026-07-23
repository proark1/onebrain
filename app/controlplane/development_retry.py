"""Self-healing development pipeline (roadmap Gap A, Phase 1).

A development candidate that fails verification parks at ``dev_failed`` and stays
there until an operator hand-runs ``retry-dev`` — even when the failure was a
transient hiccup (a gate that was briefly busy, a rollout/dispatch blip, a stale
gate binding, a schema update waiting on the daily backup) that a later identical
attempt would clear on its own. This module reclaims those candidates: on the
Mission-Control reconcile tick it re-dispatches the ones whose failure is
*transient*, bounded by an attempt cap + backoff, and records a durable give-up
alert when a candidate exhausts its budget.

The whole thing is policy over the existing promotion machinery — it adds no state
machine and no new terminal state. It re-dispatches through the SAME
``_dispatch_development_candidate`` path ``retry-dev`` uses, tagged with a distinct
actor so auto-retries are countable and never confused with operator retries.

**It never touches the security firebreak.** Only development candidates are
considered (never a ``customer_approved`` / ``customer_paused`` release), and only
*transient* reasons are retried. A genuinely bad build (invalid signature, wrong
module set, a heartbeat that came back unhealthy/mismatched) is PERMANENT: it is
left for a human, because re-running the same build yields the same failure.
Anything unclassified is treated as permanent — auto-retry fails safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.trust.envelope import compare_versions

_log = logging.getLogger("onebrain.fleet")

# The auto-retry dispatcher stamps this actor on the promotion event it creates, so
# its attempts are distinguishable from an operator's manual retry-dev (which carries
# the operator's user id) and from the initial CI dispatch (actor "mission-control").
AUTO_RETRY_ACTOR = "mission-control:auto-retry"

# Actions _dispatch_development_candidate emits when it actually starts an attempt.
_DISPATCH_ACTIONS = frozenset({"dev_rollout_started", "dev_rollout_retried"})

# Durable, timeline-visible marker recorded once when a candidate gives up. Rides
# the existing promotion-event log (surfaced by GET /api/operator/releases), so the
# give-up is legible in the console without a log grep — the Phase-1 "Gap D-lite"
# alert surface. A real push channel is Phase 3 (Gap D).
AUTO_RETRY_EXHAUSTED_ACTION = "dev_auto_retry_exhausted"

# A preflight denial is recorded generically under this failure_reason; the specific
# plan reason lives in the final event note (see _fail_development_preflight and
# is_current_replacement_bootstrap_failure, which reads events[-1].note the same way).
_PREFLIGHT_FAILURE_REASON = "dev_preflight_failed"

# --- Transient/permanent classification (roadmap fork #1 — the crux) -------------
#
# TRANSIENT = a re-dispatch of the SAME build can succeed with no code change and no
# human, because the failure was environmental/timing, not the build. Split into two
# cadences: an infra hiccup should retry soon; a schema update waiting on the daily
# 02:30 backup should retry on a slow cadence that rides THROUGH that backup rather
# than burning its whole budget in minutes.

# Retry soon (infra / timing / stale-binding hiccups).
RETRY_NOW_REASONS = frozenset({
    "dev_rollout_failed",            # rollout callback reported failed (often a gate hiccup)
    "dev_dispatch_failed",           # the broker could not dispatch the child rollout
    "dev_convergence_timeout",       # the pull rollout did not converge inside the window
    "dev_verification_timeout",      # rollout succeeded; the follow-up heartbeat was late
    "dev_heartbeat_time_invalid",    # a transient clock/timestamp parse blip
    "dev_secrets_epoch_mismatch",    # the box is still applying a just-rotated bundle
    "dev_secrets_epoch_invalid",     # transient payload parse of the required epoch
    "development_gate_missing",      # briefly between gates (no live gate right now)
    "development_gate_mismatch",     # stale gate binding (mostly #55-fixed in-planner)
    "deployment_rollout_active",     # the gate is busy finishing another rollout
    "deployment_heartbeat_stale",    # the gate has gone briefly quiet
    "deployment_unhealthy",          # the gate is transiently unhealthy / recovering
})

# Transient but cleared only by a SCHEDULED external event: a migration-crossing
# update needs a fresh successful backup, and the gate takes one daily at 02:30 UTC.
# Retried on a slow cadence so it survives until that backup lands.
AWAIT_BACKUP_REASONS = frozenset({
    "backup_required_for_schema_update",
})

TRANSIENT_REASONS = RETRY_NOW_REASONS | AWAIT_BACKUP_REASONS

# PERMANENT (never auto-retried — kept here as documentation; anything NOT transient
# is permanent by default): release_signature_invalid, release_unsigned, release_yanked,
# release_not_dev_verified, release_not_customer_approved, release_customer_paused,
# the module-set/gate-replacement reasons (development_gate_target_module_set_invalid,
# development_gate_current_module_set_invalid, development_gate_replacement_required),
# and every heartbeat verification mismatch (dev_heartbeat_unhealthy, dev_version_mismatch,
# dev_migration_mismatch, dev_attempt_mismatch, dev_target_mismatch, dev_module_*). These
# all reproduce on an identical retry and need a new build, a gate replacement, or an
# operator decision.


def effective_failure_reason(promotion, events) -> str:
    """The actionable reason behind a ``dev_failed`` promotion.

    Preflight denials are recorded generically as ``dev_preflight_failed`` with the
    real plan reason in the final event note; unwrap that so classification keys on
    the specific reason (mirrors ``is_current_replacement_bootstrap_failure``).
    """
    reason = (getattr(promotion, "failure_reason", "") or "").strip()
    if reason == _PREFLIGHT_FAILURE_REASON and events:
        note = (getattr(events[-1], "note", "") or "").strip()
        return note or reason
    return reason


def is_transient_development_failure(reason: str) -> bool:
    return reason in TRANSIENT_REASONS


def _parse_ts(value: str):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _auto_retry_dispatch_indices(events) -> list[int]:
    return [
        i for i, event in enumerate(events)
        if getattr(event, "actor", "") == AUTO_RETRY_ACTOR
        and getattr(event, "action", "") in _DISPATCH_ACTIONS
    ]


def _already_alerted(events) -> bool:
    """Whether a give-up alert was already recorded for the CURRENT failure episode.

    Dedup horizon is the last auto-retry dispatch: an exhausted marker after it means
    we already alerted this episode. A later operator retry-dev (a non-auto dispatch)
    does not re-arm the alert — once auto-retry gives up, the human owns it.
    """
    dispatches = _auto_retry_dispatch_indices(events)
    if not dispatches:
        return False
    after = dispatches[-1]
    return any(
        getattr(event, "action", "") == AUTO_RETRY_EXHAUSTED_ACTION
        for event in events[after + 1:]
    )


@dataclass(frozen=True)
class AutoRetryDecision:
    """What the policy decided for one candidate this tick."""
    action: str                 # "retry" | "wait" | "give_up" | "skip"
    version: str
    reason: str = ""            # the effective failure reason
    attempts: int = 0           # auto-retries already spent
    already_alerted: bool = False


def plan_development_auto_retry(
    promotion,
    events,
    *,
    now: datetime,
    max_attempts: int,
    backoff_seconds: int,
    backup_backoff_seconds: int,
) -> AutoRetryDecision:
    """Pure policy: decide this candidate's fate without side effects.

    ``skip``   — not our business (not dev_failed, or a permanent failure a human owns).
    ``retry``  — transient, within budget, backoff elapsed → re-dispatch.
    ``wait``   — transient, within budget, still inside the backoff window.
    ``give_up`` — transient but the attempt budget is exhausted → alert, stop.
    """
    version = getattr(promotion, "release_version", "")
    if getattr(promotion, "state", "") != "dev_failed":
        return AutoRetryDecision("skip", version)
    reason = effective_failure_reason(promotion, events)
    if not is_transient_development_failure(reason):
        # Permanent (or unclassified → fail safe): never auto-retry.
        return AutoRetryDecision("skip", version, reason=reason)

    attempts = len(_auto_retry_dispatch_indices(events))
    if attempts >= max(1, int(max_attempts)):
        return AutoRetryDecision(
            "give_up", version, reason=reason, attempts=attempts,
            already_alerted=_already_alerted(events),
        )

    backoff = backup_backoff_seconds if reason in AWAIT_BACKUP_REASONS else backoff_seconds
    last_at = _parse_ts(getattr(events[-1], "created_at", "")) if events else None
    if last_at is not None and (now - last_at).total_seconds() < max(0, int(backoff)):
        return AutoRetryDecision("wait", version, reason=reason, attempts=attempts)
    return AutoRetryDecision("retry", version, reason=reason, attempts=attempts)


def _newest_verified_version(promotions) -> str:
    """The highest version the gate has already VERIFIED (dev_verified or later)."""
    best = ""
    for promotion in promotions:
        if getattr(promotion, "state", "") not in {"dev_verified", "customer_approved"}:
            continue
        version = promotion.release_version
        if not best or (compare_versions(version, best) or 0) > 0:
            best = version
    return best


def _is_superseded(version: str, newest_verified: str) -> bool:
    """Whether a newer release has already verified, making this candidate moot.

    Fails safe: an incomparable pair (compare_versions -> None) is NOT superseded, so
    an odd version string is still eligible rather than silently dropped.
    """
    if not newest_verified:
        return False
    comparison = compare_versions(version, newest_verified)
    return comparison is not None and comparison <= 0


def reclaim_retryable_development_candidates(
    store,
    *,
    now: datetime,
    max_attempts: int,
    backoff_seconds: int,
    backup_backoff_seconds: int,
    dispatch,
    log: logging.Logger | None = None,
) -> list[AutoRetryDecision]:
    """Re-dispatch every transiently-failed development candidate that is due.

    ``dispatch`` is injected — ``app.routers.operator._dispatch_development_candidate``
    in production (imported lazily by the caller to avoid a router-import cycle), a
    stub in tests. Every candidate is isolated: one that raises is logged and skipped,
    never allowed to break the tick. Returns the per-candidate decisions (for logging
    and tests).

    Candidates already SUPERSEDED by a newer verified release are skipped entirely — not
    retried and not alerted — so auto-retry never burns gate cycles or raises give-up
    noise on a build the pipeline has already moved past (roadmap fork #2: newest wins).
    An operator can still hand-run retry-dev on any candidate.
    """
    logger = log or _log
    promotions = store.list_release_promotions()
    newest_verified = _newest_verified_version(promotions)
    decisions: list[AutoRetryDecision] = []
    for promotion in promotions:
        if getattr(promotion, "state", "") != "dev_failed":
            continue
        version = promotion.release_version
        if _is_superseded(version, newest_verified):
            decisions.append(AutoRetryDecision("skip", version, reason="superseded"))
            continue
        events = store.list_release_promotion_events(version)
        decision = plan_development_auto_retry(
            promotion, events, now=now,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            backup_backoff_seconds=backup_backoff_seconds,
        )
        decisions.append(decision)
        if decision.action == "retry":
            try:
                dispatch(store, version, actor=AUTO_RETRY_ACTOR)
                logger.info(
                    "development auto-retry re-dispatched %s (reason=%s, attempt=%d/%d)",
                    version, decision.reason, decision.attempts + 1, max(1, int(max_attempts)),
                )
            except Exception as exc:  # one bad candidate must never break the tick
                logger.warning("development auto-retry could not dispatch %s: %s", version, exc)
        elif decision.action == "give_up" and not decision.already_alerted:
            _record_give_up(store, decision, logger)
    return decisions


def _record_give_up(store, decision: AutoRetryDecision, logger: logging.Logger) -> None:
    """Record the durable give-up marker + emit the operator-facing warning, once."""
    try:
        store.record_release_promotion_event(
            decision.version,
            action=AUTO_RETRY_EXHAUSTED_ACTION,
            actor=AUTO_RETRY_ACTOR,
            note=decision.reason,
            metadata={"attempts": decision.attempts, "reason": decision.reason},
        )
    except Exception as exc:  # a failed marker must not suppress the log alert
        logger.warning("development auto-retry could not record give-up for %s: %s",
                       decision.version, exc)
    logger.warning(
        "development auto-retry exhausted for %s after %d attempts (reason=%s) "
        "— operator attention required",
        decision.version, decision.attempts, decision.reason,
    )

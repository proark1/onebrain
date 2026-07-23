"""Auto-replace a sustained-dead development release gate (roadmap Phase 4, Gap E2, Tier 2).

When the DESIGNATED development gate dies (the 2026-07-22 disk-full incident), the whole
release pipeline stalls: nothing new can ``dev_verify`` until a human rebuilds the gate.
Tier 1 (``app/fleet/pipeline_watchdog.py``) turns that into a ``gate_replacement_recommended``
alert. Tier 2 — this module — is the opt-in that ACTS on the very same signal: it provisions a
replacement gate through the broker and, once the box boots healthy and passes the manual
designation preflight, designates it, so the pipeline self-heals with no operator action.

**This is the roadmap's riskiest automation: the only one that provisions billable
infrastructure.** Three properties keep it safe:

1. **It never tears anything down.** The destructive step (decommissioning the old, now-
   undesignated gate) STAYS a human action — the daemon only opens a
   ``gate_decommission_recommended`` alert prompting it. That is the whole difference between
   Tier 2 (this) and Tier 3 (rejected): Tier 2 never touches ``/v1/destroy``, the P1-D guard,
   or the dual-control teardown bar.
2. **It rides the existing guards, weakening none.** Provisioning goes through the same
   ``provision_development_gate`` path an operator uses — so the broker still holds the Hetzner
   token, still enforces the server cap, and ``_development_gate_identity`` still refuses a
   second live replacement (one-in-flight). Designation runs the same ``_development_gate_blockers``
   preflight and the atomic strictly-one-gate store swap.
3. **The cost-runaway rails are load-bearing, not decorative** (fork B / the #1 risk): a
   *sustained-ness* window (``gate_replace_after_seconds`` — the same one Tier 1 alerts on),
   *one-in-flight* (never provision while a replacement is in flight), a *min-interval* between
   provision attempts, and an MC-side *cap headroom* pre-check — with the broker's hard cap as
   the final backstop. A flapping gate can mint at most one box per min-interval, up to the cap.

Everything is **world-derived and idempotent** — there is no separate state machine. Each tick
re-derives the situation from the stores and takes at most one imperative step (provision OR
designate); a step interrupted mid-way is simply re-derived and resumed on the next tick. The
policy is a pure function (``decide_gate_replacement``); ``run_gate_auto_replace_tick`` does the
I/O and calls injected provision/designate/blocker callables (imported lazily by
``gate_auto_replace_once`` to avoid pulling the router graph in at startup), exactly like the
development-auto-retry daemon injects its dispatcher.

OFF by default (``gate_auto_replace_enabled``); the daemon does not even start otherwise.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from app.fleet.base import (
    GATE_AUTO_REPLACE_WEDGED_ALERT,
    GATE_DECOMMISSION_RECOMMENDED_ALERT,
    FleetAlert,
)
# Single source of truth for "the gate is sustained-dead": Tier 1 ALERTS on this, Tier 2 ACTS
# on it — importing the shared helper guarantees detection and action can never diverge.
from app.fleet.pipeline_watchdog import (
    GATE_HARD_FAILURE_ALERT_KINDS,
    sustained_gate_failure,
)

_log = logging.getLogger("onebrain.fleet")

# Stamped on the provisioning audit trail + on the designation dispatch, so an auto-replacement
# is never confused with an operator's manual provision/designate (which carry a user id).
GATE_AUTO_REPLACE_ACTOR = "mission-control:auto-gate-replace"

# Detail for the wedge opened when a provision reached the broker but the box never came up
# (dispatch_failed / never healthy) — a recent failed attempt row with no live replacement.
_FAILED_ATTEMPT_WEDGE = (
    "a recent auto-provision attempt reached the broker but the replacement box did not come up "
    "(dispatch failed, or it never enrolled healthy); auto-replacement is holding off until the "
    "min-interval — check the broker; it retries automatically"
)

# Provisioning-run statuses that mean the broker never created a box (so there is nothing to reap).
# Mirrors app.routers.operator._DEAD_PROVISION_RUN_STATUSES minus "failed" (a "failed" run may have
# created a box that later died — that IS reapable).
_NEVER_CREATED_RUN_STATUSES = frozenset({"dispatch_failed", "cancelled"})


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class GateReplaceDecision:
    """What one tick decided. ``action`` is at most one imperative step; ``wedge_detail`` and
    ``reap_details`` are the desired Tier-2 alert state the orchestrator reconciles."""
    action: str                                   # "noop" | "provision" | "designate"
    reason: str = ""
    wedge_detail: Optional[str] = None            # gate_auto_replace_wedged on MC's row; None = none wanted
    reap_details: Dict[str, str] = field(default_factory=dict)  # deployment_id -> decommission detail
    replacement_id: str = ""                      # designate target
    provision_region: str = ""                    # provision input


def decide_gate_replacement(
    *,
    now: datetime,
    mc_deployment_id: str,
    gate,                                          # get_release_gate() -> CustomerDeployment | None
    gate_sustained_failure: Optional[Tuple[str, float]],   # (kind, age_seconds) | None
    live_replacement,                              # the in-flight replacement row | None (one-in-flight)
    live_replacement_created_at: Optional[datetime],
    live_replacement_blockers: List[str],          # [] = designation-ready; ignored if no live replacement
    reap_details: Dict[str, str],                  # undesignated dead gate rows -> decommission detail
    last_attempt_at: Optional[datetime],           # newest replacement box's creation (min-interval anchor)
    live_deployment_count: int,
    max_fleet_servers: int,
    min_interval_seconds: int,
    replace_timeout_seconds: int,
    provisioner_ready: bool,
    owner_email_available: bool,
    provision_block_reason: Optional[str],   # a pre-row config blocker (misconfig), else None
    failed_attempt_recent: bool,             # a recent attempt reached the broker but produced no live box
    replacement_region: str,
) -> GateReplaceDecision:
    """Pure policy — no side effects, fully unit-testable without a store.

    The decommission recommendations (``reap_details``) ride EVERY decision, so a superseded dead
    gate is prompted for teardown whatever else the tick does. A ``wedge_detail`` is set only for
    world-derived, self-resolving stuck states (redundant replacement, no owner email, at server
    cap, provisioning misconfigured, a failed attempt); on any other tick it is ``None`` so the
    orchestrator resolves a stale wedge. The provisioning ladder is ordered cheapest/hardest-stop
    first. Every wedge condition is RE-EVALUATED each tick from current world/config, so provision is
    never called while it would fail pre-row (no silent retry loop) and a wedge clears the instant
    its cause does."""
    reap = dict(reap_details)

    def _decide(action: str, reason: str, *, wedge: Optional[str] = None,
                replacement_id: str = "", region: str = "") -> GateReplaceDecision:
        return GateReplaceDecision(
            action=action, reason=reason, wedge_detail=wedge, reap_details=reap,
            replacement_id=replacement_id, provision_region=region,
        )

    # A replacement is in flight but the gate is HEALTHY or gone: it was provisioned when the gate
    # was dead and the gate recovered (or was un-designated) first. It is now a redundant billable
    # box that also holds the one-in-flight slot against the NEXT real failure — surface it rather
    # than strand it silently. (Checked BEFORE the healthy-gate early return below.)
    if live_replacement is not None and (gate is None or gate_sustained_failure is None):
        return _decide(
            "noop", "redundant replacement after gate recovery",
            wedge=(
                f"replacement gate {live_replacement.id} was provisioned but the gate recovered or "
                "is no longer designated — it is redundant and holds the one-in-flight slot; "
                "designate it or decommission it"
            ),
        )

    # No designated gate, or the gate is healthy / not sustained-dead -> nothing to replace.
    # (A missing gate is deliberately NOT bootstrapped from zero: Tier 2 REPLACES a dying gate,
    # it does not stand up the first one — that stays a deliberate operator action.)
    if gate is None or gate_sustained_failure is None:
        return _decide("noop", "gate healthy or none designated")

    kind, age = gate_sustained_failure

    # A replacement is already in flight (one-in-flight rail): never mint a second box.
    if live_replacement is not None:
        if not live_replacement_blockers:
            # Healthy + passes the manual designation preflight -> promote it (step 5).
            return _decide(
                "designate",
                f"replacement {live_replacement.id} healthy after gate {kind} ({int(age)}s)",
                replacement_id=live_replacement.id,
            )
        if (
            replace_timeout_seconds > 0
            and live_replacement_created_at is not None
            and (now - live_replacement_created_at).total_seconds() > replace_timeout_seconds
        ):
            # Orphan-wedge guard: a replacement that never went healthy is blocking further
            # provisions AND sitting on a cap slot while the old gate stays designated-dead.
            # STOP and alert; do not loop (the design's explicit degrade-to-alert).
            elapsed = int((now - live_replacement_created_at).total_seconds())
            return _decide(
                "noop", "replacement wedged (timeout)",
                wedge=(
                    f"replacement gate {live_replacement.id} was not designation-ready {elapsed}s "
                    f"after provision (blockers: {','.join(live_replacement_blockers) or 'none'}); "
                    "auto-replacement STOPPED — operator attention required"
                ),
            )
        return _decide(
            "noop",
            f"waiting for replacement {live_replacement.id} "
            f"(blockers: {','.join(live_replacement_blockers) or 'none'})",
        )

    # Gate sustained-dead and NO replacement in flight -> consider provisioning one.
    if not provisioner_ready:
        # Not the Hetzner broker backend -> the daemon cannot provision. Stay quiet (the Tier 1
        # gate_replacement_recommended alert already surfaces the dead gate).
        return _decide("noop", "provisioner backend is not hetzner")
    if not owner_email_available:
        return _decide(
            "noop", "no owner email configured",
            wedge=(
                "cannot auto-provision a replacement gate: no owner email is configured "
                "(set ONEBRAIN_ADMIN_EMAIL); auto-replacement blocked"
            ),
        )
    if max_fleet_servers > 0 and live_deployment_count >= max_fleet_servers:
        # MC-side cap pre-check (courtesy; the broker enforces the real cap on billable create).
        return _decide(
            "noop", "fleet at server cap",
            wedge=(
                f"cannot auto-provision a replacement gate: fleet is at the server cap "
                f"({live_deployment_count}/{max_fleet_servers}); decommission a box or raise "
                "ONEBRAIN_HETZNER_MAX_FLEET_SERVERS"
            ),
        )
    if provision_block_reason:
        # Pre-row provisioning is blocked (misconfig: no trusted baseline/images, missing dev verify
        # key or fleet_public_url, broker not ready). World-derived + re-evaluated each tick, so
        # provision is NEVER called while it would fail before creating a box -> no silent retry loop,
        # and this clears the instant the configuration is fixed.
        return _decide(
            "noop", "provisioning blocked",
            wedge=(
                f"cannot auto-provision a replacement gate: {provision_block_reason}; "
                "auto-replacement blocked until Mission Control provisioning is fixed"
            ),
        )
    if failed_attempt_recent:
        # The last provision reached the broker but the box never came up (dispatch_failed / never
        # enrolled healthy). Surface the stuck Tier-2 state and hold off until the min-interval;
        # this both bounds the cost and is the alert operators are told to watch for.
        return _decide("noop", "recent provision attempt failed", wedge=_FAILED_ATTEMPT_WEDGE)
    if (
        min_interval_seconds > 0
        and last_attempt_at is not None
        and (now - last_attempt_at).total_seconds() < min_interval_seconds
    ):
        # Cost-runaway rail: at most one provision attempt per window. Anchors on the newest
        # replacement row, so a gate that was itself just provisioned and died is not re-provisioned
        # instantly (no wedge — a plain rate limit, distinct from the failed-attempt state above).
        elapsed = int((now - last_attempt_at).total_seconds())
        return _decide("noop", f"min-interval debounce ({elapsed}s < {min_interval_seconds}s)")

    return _decide(
        "provision",
        f"gate sustained-dead ({kind} for {int(age)}s); provisioning a replacement",
        region=replacement_region,
    )


def _reconcile_tier2_alerts(
    fleet_store, *, mc_deployment_id: str, wedge_detail: Optional[str],
    reap_details: Dict[str, str], gate_present: bool, now_iso: str, next_id: Callable[[], str],
) -> List[FleetAlert]:
    """Open/resolve ONLY the two Tier-2 alert kinds; never touches the infra or pipeline alerts.

    ``gate_auto_replace_wedged`` is a single alert on Mission Control's own row (opened when the
    daemon is stuck, resolved the moment it is not). ``gate_decommission_recommended`` is one alert
    per superseded dead gate, on that gate's OWN row; a global sweep resolves any that is no longer
    a reap candidate (reaped / recovered / tombstoned) even when the row has left list_deployments."""
    opened: List[FleetAlert] = []

    if wedge_detail is not None:
        if not fleet_store.has_open_alert(mc_deployment_id, GATE_AUTO_REPLACE_WEDGED_ALERT):
            opened.append(fleet_store.open_alert(FleetAlert(
                id=next_id(), deployment_id=mc_deployment_id,
                kind=GATE_AUTO_REPLACE_WEDGED_ALERT, detail=wedge_detail,
                status="open", created_at=now_iso,
            )))
    else:
        fleet_store.resolve_open_alerts(mc_deployment_id, GATE_AUTO_REPLACE_WEDGED_ALERT, now_iso)

    # Only reconcile reap recommendations when a gate IS designated. When get_release_gate() is
    # transiently None (mid un-designation) reap_details is empty, and running the global sweep then
    # would FLAP every open reap alert off and immediately back on — so leave them untouched until a
    # gate exists again.
    if gate_present:
        for deployment_id, detail in reap_details.items():
            if not fleet_store.has_open_alert(deployment_id, GATE_DECOMMISSION_RECOMMENDED_ALERT):
                opened.append(fleet_store.open_alert(FleetAlert(
                    id=next_id(), deployment_id=deployment_id,
                    kind=GATE_DECOMMISSION_RECOMMENDED_ALERT, detail=detail,
                    status="open", created_at=now_iso,
                )))
        for alert in fleet_store.list_open_alerts():
            if (alert.kind == GATE_DECOMMISSION_RECOMMENDED_ALERT
                    and alert.deployment_id not in reap_details):
                fleet_store.resolve_open_alerts(
                    alert.deployment_id, GATE_DECOMMISSION_RECOMMENDED_ALERT, now_iso)

    return opened


def run_gate_auto_replace_tick(
    control_store, fleet_store, run_store, *,
    now_iso: str,
    mc_deployment_id: str,
    gate_base_id: str,
    sustained_after_seconds: int,
    min_interval_seconds: int,
    replace_timeout_seconds: int,
    max_fleet_servers: int,
    owner_email: str,
    provisioner_ready: bool,
    blockers_for: Callable,               # (deployment) -> list[str]
    is_live_replacement: Callable,        # (deployment, gate, newest_run_status) -> bool
    provision_preflight: Callable,        # () -> Optional[str]  (a pre-row block reason, or None)
    provision: Callable,                  # (owner_email, region) -> None  (may raise)
    designate: Callable,                  # (deployment_id) -> None        (may raise)
    next_id: Callable[[], str],
    log: Optional[logging.Logger] = None,
) -> Tuple[GateReplaceDecision, List[FleetAlert]]:
    """One tick: derive the world, decide, reconcile the Tier-2 alerts, take at most one step.

    The operator-router helpers are INJECTED (built lazily by ``gate_auto_replace_once``) so this
    module never imports the router graph and the whole tick is unit-testable with stubs. Returns
    ``(decision, opened_alerts)`` for logging, webhook delivery, and tests."""
    logger = log or _log
    now = _parse_ts(now_iso) or datetime.now(timezone.utc)

    gate = control_store.get_release_gate()
    deployments = list(control_store.list_deployments())
    gate_rows = [
        deployment for deployment in deployments
        if deployment.id == gate_base_id or deployment.id.startswith(gate_base_id + "-")
    ]

    def _newest_run_status(deployment_id: str) -> str:
        runs = run_store.list_runs(deployment_id=deployment_id)
        return runs[0].status if runs else ""

    # One-in-flight: at most one live (undesignated, not-dead) replacement row.
    live_replacement = next(
        (deployment for deployment in gate_rows
         if is_live_replacement(deployment, gate, _newest_run_status(deployment.id))),
        None,
    )

    # Reap candidates: an undesignated dead GATE that is safe to tear down by hand. Matched by gate
    # SHAPE (a development dedicated_server) rather than the id prefix, so a gate that was designated
    # from `prepare-existing` / a manual non-standard id is reaped too once auto-replacement moves
    # off it. A pure dispatch-failed attempt (never a real box) is excluded — there is nothing to
    # decommission. list_deployments already hides tombstoned rows.
    reap_details: Dict[str, str] = {}
    if gate is not None:
        for deployment in deployments:
            if deployment.id == gate.id:
                continue
            if live_replacement is not None and deployment.id == live_replacement.id:
                continue
            is_gate_shaped = (
                deployment.id == gate_base_id or deployment.id.startswith(gate_base_id + "-")
                or (getattr(deployment, "environment", "") == "development"
                    and getattr(deployment, "deployment_type", "") == "dedicated_server")
            )
            if not is_gate_shaped:
                continue
            if _newest_run_status(deployment.id) in _NEVER_CREATED_RUN_STATUSES:
                continue   # a failed dispatch never became a box -> nothing to reap
            if any(alert.kind in GATE_HARD_FAILURE_ALERT_KINDS
                   for alert in fleet_store.list_open_alerts(deployment.id)):
                reap_details[deployment.id] = (
                    f"development gate {deployment.id} is undesignated and dead; safe to "
                    "decommission by hand (Tier 2 never tears down automatically)"
                )

    gate_sustained = (
        sustained_gate_failure(fleet_store, gate.id, now, sustained_after_seconds)
        if (gate is not None and sustained_after_seconds > 0) else None
    )
    considering_provision = gate_sustained is not None and live_replacement is None

    live_blockers = blockers_for(live_replacement) if live_replacement is not None else []
    live_created = _parse_ts(live_replacement.created_at) if live_replacement is not None else None

    # min-interval anchors on the newest SUFFIXED gate row's creation — every billable provision
    # attempt (success, dispatch failure, or broker cap rejection) creates such a row, so this
    # bounds attempts even when they fail. The bare base id is the ORIGINAL gate, never an anchor.
    suffix_times = [
        _parse_ts(deployment.created_at) for deployment in gate_rows
        if deployment.id.startswith(gate_base_id + "-")
    ]
    last_attempt_at = max([t for t in suffix_times if t is not None], default=None)

    # A recent suffixed row that is NEITHER the designated gate NOR the (single) live replacement is
    # a provision attempt that reached the broker but produced no healthy box (dispatch_failed, or a
    # box that never enrolled). Distinct from the plain rate-limit debounce so it surfaces a wedge.
    failed_attempt_recent = (
        min_interval_seconds > 0 and gate is not None
        and any(
            deployment.id != gate.id
            and (live_replacement is None or deployment.id != live_replacement.id)
            and _parse_ts(deployment.created_at) is not None
            and (now - _parse_ts(deployment.created_at)).total_seconds() < min_interval_seconds
            for deployment in gate_rows
            if deployment.id.startswith(gate_base_id + "-")
        )
    )

    replacement_region = (getattr(gate, "region", "") or "nbg1") if gate is not None else "nbg1"

    decision = decide_gate_replacement(
        now=now, mc_deployment_id=mc_deployment_id, gate=gate,
        gate_sustained_failure=gate_sustained, live_replacement=live_replacement,
        live_replacement_created_at=live_created, live_replacement_blockers=live_blockers,
        reap_details=reap_details, last_attempt_at=last_attempt_at,
        live_deployment_count=len(deployments), max_fleet_servers=max_fleet_servers,
        min_interval_seconds=min_interval_seconds, replace_timeout_seconds=replace_timeout_seconds,
        provisioner_ready=provisioner_ready, owner_email_available=bool(owner_email.strip()),
        # Only pay for the (dry-run) provisioning preflight when we might actually provision.
        provision_block_reason=(provision_preflight() if considering_provision else None),
        failed_attempt_recent=failed_attempt_recent,
        replacement_region=replacement_region,
    )

    opened = _reconcile_tier2_alerts(
        fleet_store, mc_deployment_id=mc_deployment_id, wedge_detail=decision.wedge_detail,
        reap_details=decision.reap_details, gate_present=gate is not None,
        now_iso=now_iso, next_id=next_id,
    )

    # Take the one imperative step, isolated: a failure is logged and the tick still returns cleanly
    # (the next tick re-derives). Pre-row rejections are already pre-checked (provision_block_reason),
    # so a provision here is expected to reach the broker; it is asynchronous, so this is a SUBMISSION,
    # not a confirmed box — a dispatch failure surfaces next tick via failed_attempt_recent.
    if decision.action == "provision":
        try:
            provision(owner_email, decision.provision_region)
            logger.info("gate auto-replace: submitted a replacement provision (%s)", decision.reason)
        except Exception as exc:  # billable path — never let it crash the daemon
            logger.warning("gate auto-replace: provision submission failed: %s", exc)
    elif decision.action == "designate":
        try:
            designate(decision.replacement_id)
            logger.info("gate auto-replace: designated replacement %s", decision.replacement_id)
        except Exception as exc:
            logger.warning(
                "gate auto-replace: could not designate %s: %s", decision.replacement_id, exc)

    return decision, opened


def _daemon_admin_principal():
    """A minimal in-process ADMIN principal for the daemon's own ``provision_development_gate`` call.

    This is NOT authentication — no request, cookie, or session is involved, and it is never
    reachable from an HTTP path. The daemon runs only inside Mission Control's own process, only
    when the operator opted into ``gate_auto_replace_enabled``, and only calls provision through the
    guards in ``decide_gate_replacement``. The provision impl reads solely ``principal.user_id``
    (stamped on the provisioning audit trail) and asserts ``role_id == "admin"``; this constructs
    exactly that, with a self-identifying actor id, mirroring the admin principal the operator
    endpoints are tested with."""
    from app.auth.principal import Principal
    from app.auth.roles import ROLES

    role = ROLES["admin"]
    return Principal(
        user_id=GATE_AUTO_REPLACE_ACTOR, role_id=role.id, role_label=role.label,
        clearance=role.clearance, locations=None, categories=role.categories,
        location_label="all",
    )


def _push_alert_webhook(settings, opened: List[FleetAlert]) -> None:
    """Deliver newly-opened Tier-2 alerts to the configured webhook (roadmap Gap D), same channel
    as the watchdog. Dormant until fleet_alert_webhook_url is set; push_open_alerts never raises."""
    url = (getattr(settings, "fleet_alert_webhook_url", "") or "").strip()
    if not url or not opened:
        return
    from app.fleet.alert_notify import push_open_alerts

    push_open_alerts(url, opened)


def gate_auto_replace_once(settings, control_store, fleet_store, run_store) -> List[FleetAlert]:
    """One never-raising tick. Builds the real operator-router callables (lazy import, so importing
    this module at startup never pulls the router graph — mirroring the reconcile daemon) and runs
    ``run_gate_auto_replace_tick``. Returns the alerts it opened (for the webhook + tests)."""
    if not getattr(settings, "operator_mode", False):
        return []
    if not getattr(settings, "gate_auto_replace_enabled", False):
        return []
    mc_deployment_id = (getattr(settings, "deployment_id", "") or "").strip()
    if not mc_deployment_id:
        return []

    try:
        from app.routers.operator import (
            DEVELOPMENT_GATE_DEPLOYMENT_ID,
            DevelopmentGateProvisionIn,
            _development_gate_blockers,
            _is_live_gate_replacement,
            dispatch_waiting_development_candidate,
            provision_development_gate,
        )
    except Exception as exc:  # a router-import hiccup must never crash the daemon
        _log.warning("gate auto-replace: operator import failed: %s", exc)
        return []

    owner_email = (getattr(settings, "admin_email", "") or "").strip()
    provisioner_ready = getattr(settings, "provisioner_backend", "") == "hetzner"

    def _blockers_for(deployment) -> list:
        return _development_gate_blockers(control_store, deployment)

    def _provision_preflight() -> Optional[str]:
        # Mirror provision_development_gate's FULL pre-row validation so provision is only attempted
        # when it will actually reach the broker (no silent retry loop, Codex L355). The endpoint's
        # DRY-RUN runs the backend / one-in-flight / trusted-baseline / image / registry-allowlist
        # checks WITHOUT creating a box; the dev-key, callback, and broker-readiness checks sit AFTER
        # the dry-run early return, so verify those explicitly. Returns a short, leak-free reason or
        # None. A dev/non-hetzner MC returns None here and is handled by provisioner_ready upstream.
        try:
            provision_development_gate(
                DevelopmentGateProvisionIn(
                    owner_email=(owner_email or "auto-replace@onebrain.local"), dry_run=True),
                _daemon_admin_principal(),
            )
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
            return str(detail)[:180]
        if not (getattr(settings, "dev_release_verify_public_key", "") or "").strip():
            return "Mission Control development release verification key is required"
        if not (getattr(settings, "fleet_public_url", "") or "").strip():
            return "Mission Control fleet_public_url is required"
        try:
            settings.assert_production_mission_control_ready()
        except Exception as exc:
            return f"Mission Control is not provisioning-ready ({str(exc)[:140]})"
        return None

    def _provision(email: str, region: str) -> None:
        # Reuses the FULL operator provision path: identity/one-in-flight, trusted baseline, image
        # allowlist, dev key, callback — and the broker (token isolation + server cap) beneath it.
        provision_development_gate(
            DevelopmentGateProvisionIn(owner_email=email, region=region, dry_run=False),
            _daemon_admin_principal(),
        )

    def _designate(deployment_id: str) -> None:
        candidate = control_store.get_deployment(deployment_id)
        if candidate is None:
            raise ValueError(f"replacement {deployment_id} vanished before designation")
        # Re-run the manual endpoint's preflight at the designation boundary (TOCTOU): the world
        # may have shifted since the decision. designate_release_gate is atomic + strictly-one-gate.
        blockers = _development_gate_blockers(control_store, candidate)
        if blockers:
            raise ValueError(f"replacement {deployment_id} not ready: {','.join(blockers)}")
        control_store.designate_release_gate(deployment_id)
        dispatch_waiting_development_candidate(control_store, actor=GATE_AUTO_REPLACE_ACTOR)

    try:
        _decision, opened = run_gate_auto_replace_tick(
            control_store, fleet_store, run_store,
            now_iso=datetime.now(timezone.utc).isoformat(),
            mc_deployment_id=mc_deployment_id,
            gate_base_id=DEVELOPMENT_GATE_DEPLOYMENT_ID,
            sustained_after_seconds=int(getattr(settings, "gate_replace_after_seconds", 0) or 0),
            min_interval_seconds=int(getattr(settings, "gate_auto_replace_min_interval_seconds", 0) or 0),
            replace_timeout_seconds=int(getattr(settings, "gate_auto_replace_timeout_seconds", 0) or 0),
            max_fleet_servers=int(getattr(settings, "hetzner_max_fleet_servers", 0) or 0),
            owner_email=owner_email, provisioner_ready=provisioner_ready,
            blockers_for=_blockers_for, is_live_replacement=_is_live_gate_replacement,
            provision_preflight=_provision_preflight, provision=_provision, designate=_designate,
            next_id=lambda: f"fa_{uuid4().hex}",
        )
    except Exception as exc:  # the whole tick fails safe -> no alerts, no crash
        _log.warning("gate auto-replace tick failed: %s", exc)
        return []

    _push_alert_webhook(settings, opened)
    return opened


def start_gate_auto_replace_scheduler(settings) -> bool:
    """Daemon: run the auto-replace tick every ``gate_auto_replace_poll_seconds``.

    Mission Control only, and DOUBLY opt-in: it returns False (no thread) unless ``operator_mode``
    AND ``gate_auto_replace_enabled`` are set AND the poll interval is > 0 — so a stock MC never
    provisions. The interval is floor-clamped to 60s. Never fatal: each tick's failure is isolated
    inside ``gate_auto_replace_once``."""
    if not getattr(settings, "operator_mode", False):
        return False
    if not getattr(settings, "gate_auto_replace_enabled", False):
        return False
    if int(getattr(settings, "gate_auto_replace_poll_seconds", 0) or 0) <= 0:
        return False
    interval = max(60, int(settings.gate_auto_replace_poll_seconds))

    def _loop() -> None:
        from app.deps import (
            get_control_plane_store,
            get_fleet_store,
            get_provisioning_run_store,
        )

        while True:
            try:
                gate_auto_replace_once(
                    settings, get_control_plane_store(), get_fleet_store(),
                    get_provisioning_run_store(),
                )
            except Exception as exc:  # pragma: no cover - defensive daemon boundary
                _log.warning("gate auto-replace tick failed: %s", exc)
            time.sleep(interval)

    threading.Thread(target=_loop, name="gate-auto-replace", daemon=True).start()
    _log.info("Gate auto-replace scheduler started (every %ss).", interval)
    return True

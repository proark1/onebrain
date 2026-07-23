"""Tests for the self-healing development pipeline (roadmap Gap A, Phase 1).

Two layers:
  * pure policy — the transient/permanent classification (fork #1), the attempt cap,
    the backoff, and the preflight-note unwrap — exercised directly on the frozen
    dataclasses so every reason and boundary is pinned exactly;
  * the orchestrator — dispatch injection, give-up recording + dedup, and per-candidate
    isolation — over a small fake store, plus one test that the real memory store's
    non-transition event append behaves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.controlplane.base import ReleasePromotion, ReleasePromotionEvent
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.development_retry import (
    AUTO_RETRY_ACTOR,
    AUTO_RETRY_EXHAUSTED_ACTION,
    AWAIT_BACKUP_REASONS,
    RETRY_NOW_REASONS,
    AutoRetryDecision,
    effective_failure_reason,
    is_transient_development_failure,
    plan_development_auto_retry,
    reclaim_retryable_development_candidates,
)

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _ago(**kwargs) -> str:
    return (NOW - timedelta(**kwargs)).isoformat()


def _promo(version="2026.07.22.501", *, state="dev_failed", failure_reason="dev_rollout_failed"):
    return ReleasePromotion(
        release_version=version,
        state=state,
        failure_reason=failure_reason,
        gate_deployment_id="gate_new",
        created_at="2026-07-20T00:00:00+00:00",
    )


def _event(version, *, action, actor="", note="", created_at):
    return ReleasePromotionEvent(
        id=f"{version}:{action}:{created_at}",
        release_version=version,
        action=action,
        actor=actor,
        note=note,
        from_state="dev_deploying",
        to_state="dev_failed",
        created_at=created_at,
    )


def _auto_dispatch(version, *, created_at):
    """One spent auto-retry attempt: the dev_deploying event our dispatcher stamps."""
    return ReleasePromotionEvent(
        id=f"{version}:auto:{created_at}",
        release_version=version,
        action="dev_rollout_retried",
        actor=AUTO_RETRY_ACTOR,
        from_state="dev_failed",
        to_state="dev_deploying",
        created_at=created_at,
    )


def _plan(promotion, events, *, max_attempts=5, backoff=600, backup_backoff=21600):
    return plan_development_auto_retry(
        promotion, events, now=NOW,
        max_attempts=max_attempts, backoff_seconds=backoff, backup_backoff_seconds=backup_backoff,
    )


# --- Classification (fork #1) -------------------------------------------------

def test_representative_transient_reasons_classify_transient():
    for reason in ["dev_rollout_failed", "dev_dispatch_failed", "dev_convergence_timeout",
                   "development_gate_mismatch", "backup_required_for_schema_update"]:
        assert is_transient_development_failure(reason) is True


def test_permanent_and_unknown_reasons_are_not_transient():
    # Genuinely-bad-build / operator / gate-replacement reasons — never auto-retried.
    for reason in ["release_signature_invalid", "release_unsigned", "release_yanked",
                   "dev_heartbeat_unhealthy", "dev_version_mismatch", "dev_migration_mismatch",
                   "development_gate_target_module_set_invalid",
                   "development_gate_current_module_set_invalid",
                   "development_gate_replacement_required",
                   "", "something_new_we_never_classified"]:
        assert is_transient_development_failure(reason) is False


def test_backup_reason_is_its_own_cadence_class():
    assert AWAIT_BACKUP_REASONS.isdisjoint(RETRY_NOW_REASONS)
    assert "backup_required_for_schema_update" in AWAIT_BACKUP_REASONS


# --- Effective reason (preflight-note unwrap) ---------------------------------

def test_effective_reason_unwraps_generic_preflight_to_the_event_note():
    promo = _promo(failure_reason="dev_preflight_failed")
    events = [_event(promo.release_version, action="dev_preflight_failed",
                     actor="mission-control", note="backup_required_for_schema_update",
                     created_at=_ago(minutes=1))]
    assert effective_failure_reason(promo, events) == "backup_required_for_schema_update"


def test_effective_reason_uses_failure_reason_directly_when_not_a_preflight_wrapper():
    promo = _promo(failure_reason="dev_rollout_failed")
    events = [_event(promo.release_version, action="dev_rollout_failed",
                     note="ignored when failure_reason is specific", created_at=_ago(minutes=1))]
    assert effective_failure_reason(promo, events) == "dev_rollout_failed"


# --- plan_development_auto_retry ---------------------------------------------

def test_plan_skips_a_candidate_that_is_not_dev_failed():
    assert _plan(_promo(state="dev_deploying"), []).action == "skip"
    assert _plan(_promo(state="dev_verified"), []).action == "skip"


def test_plan_skips_a_permanent_failure_so_a_human_owns_it():
    promo = _promo(failure_reason="release_signature_invalid")
    decision = _plan(promo, [])
    assert decision.action == "skip"
    assert decision.reason == "release_signature_invalid"


def test_plan_retries_a_transient_failure_once_backoff_has_elapsed():
    promo = _promo(failure_reason="dev_rollout_failed")
    events = [_event(promo.release_version, action="dev_rollout_failed", created_at=_ago(minutes=20))]
    decision = _plan(promo, events, backoff=600)
    assert decision.action == "retry"
    assert decision.attempts == 0


def test_plan_waits_while_inside_the_backoff_window():
    promo = _promo(failure_reason="dev_rollout_failed")
    events = [_event(promo.release_version, action="dev_rollout_failed", created_at=_ago(minutes=1))]
    assert _plan(promo, events, backoff=600).action == "wait"


def test_backup_wait_uses_the_slow_cadence_not_the_infra_backoff():
    # A migration-crosser parks behind the daily backup, recorded generically as
    # dev_preflight_failed with the real reason in the note.
    promo = _promo(failure_reason="dev_preflight_failed")
    # 30 min old: past the 10-min infra backoff, but well inside the 6-hour backup cadence.
    recent = [_event(promo.release_version, action="dev_preflight_failed",
                     note="backup_required_for_schema_update", created_at=_ago(minutes=30))]
    assert _plan(promo, recent, backoff=600, backup_backoff=21600).action == "wait"
    # 7 hours old: past the backup cadence too → retry (rides through the 02:30 backup).
    old = [_event(promo.release_version, action="dev_preflight_failed",
                  note="backup_required_for_schema_update", created_at=_ago(hours=7))]
    assert _plan(promo, old, backoff=600, backup_backoff=21600).action == "retry"


def test_plan_gives_up_once_the_attempt_budget_is_exhausted():
    promo = _promo(failure_reason="dev_rollout_failed")
    events = [_auto_dispatch(promo.release_version, created_at=_ago(minutes=30 * (5 - i)))
              for i in range(5)]
    decision = _plan(promo, events, max_attempts=5)
    assert decision.action == "give_up"
    assert decision.attempts == 5
    assert decision.already_alerted is False


def test_give_up_reports_already_alerted_when_a_marker_follows_the_last_attempt():
    promo = _promo(failure_reason="dev_rollout_failed")
    events = [_auto_dispatch(promo.release_version, created_at=_ago(hours=3))
              for _ in range(5)]
    events.append(_event(promo.release_version, action=AUTO_RETRY_EXHAUSTED_ACTION,
                         actor=AUTO_RETRY_ACTOR, created_at=_ago(hours=2)))
    assert _plan(promo, events, max_attempts=5).already_alerted is True


def test_operator_retries_do_not_burn_the_auto_retry_budget():
    promo = _promo(failure_reason="dev_rollout_failed")
    # Four operator retries (human actor) + one auto retry → only the auto one counts.
    events = [_event(promo.release_version, action="dev_rollout_retried", actor="operator-42",
                     created_at=_ago(hours=6)) for _ in range(4)]
    events.append(_auto_dispatch(promo.release_version, created_at=_ago(hours=5)))
    decision = _plan(promo, events, max_attempts=5)
    assert decision.action == "retry"
    assert decision.attempts == 1


# --- Orchestrator -------------------------------------------------------------

class _FakeStore:
    def __init__(self, promotions, events):
        self._promotions = list(promotions)
        self._events = {v: list(evs) for v, evs in events.items()}
        self.recorded: list[ReleasePromotionEvent] = []

    def list_release_promotions(self):
        return list(self._promotions)

    def list_release_promotion_events(self, version):
        return list(self._events.get(version, []))

    def record_release_promotion_event(self, version, *, action, actor="", note="", metadata=None):
        event = ReleasePromotionEvent(
            id=f"rec-{len(self.recorded) + 1}", release_version=version, action=action,
            actor=actor, note=note, metadata=dict(metadata or {}),
            from_state="dev_failed", to_state="dev_failed", created_at=NOW.isoformat(),
        )
        self._events.setdefault(version, []).append(event)
        self.recorded.append(event)
        return event


class _DispatchSpy:
    def __init__(self, raises_for=()):
        self.calls: list[tuple[str, str]] = []
        self._raises_for = set(raises_for)

    def __call__(self, store, version, *, actor):
        self.calls.append((version, actor))
        if version in self._raises_for:
            raise RuntimeError("dispatch blew up")


def _reclaim(store, dispatch, **kwargs):
    return reclaim_retryable_development_candidates(
        store, now=NOW, max_attempts=kwargs.get("max_attempts", 5),
        backoff_seconds=kwargs.get("backoff", 600),
        backup_backoff_seconds=kwargs.get("backup_backoff", 21600),
        dispatch=dispatch,
    )


def test_orchestrator_redispatches_eligible_and_leaves_permanent_alone():
    good = _promo("2026.07.22.486", failure_reason="dev_rollout_failed")
    bad = _promo("2026.07.22.490", failure_reason="release_signature_invalid")
    settled = _promo("2026.07.22.468", state="dev_verified")
    store = _FakeStore(
        [good, bad, settled],
        {good.release_version: [_event(good.release_version, action="dev_rollout_failed",
                                       created_at=_ago(minutes=30))]},
    )
    spy = _DispatchSpy()
    _reclaim(store, spy)
    assert spy.calls == [(good.release_version, AUTO_RETRY_ACTOR)]
    assert store.recorded == []  # nothing exhausted → no give-up marker


def test_orchestrator_records_the_give_up_marker_exactly_once():
    version = "2026.07.22.501"
    promo = _promo(version, failure_reason="dev_rollout_failed")
    events = {version: [_auto_dispatch(version, created_at=_ago(hours=6 - i)) for i in range(5)]}
    store = _FakeStore([promo], events)
    spy = _DispatchSpy()

    first = _reclaim(store, spy, max_attempts=5)
    assert [d.action for d in first] == ["give_up"]
    assert len(store.recorded) == 1
    assert store.recorded[0].action == AUTO_RETRY_EXHAUSTED_ACTION
    assert store.recorded[0].note == "dev_rollout_failed"
    assert spy.calls == []  # exhausted → never dispatched

    # Second tick sees the marker it just wrote → does NOT alert again.
    second = _reclaim(store, spy, max_attempts=5)
    assert [d.action for d in second] == ["give_up"]
    assert second[0].already_alerted is True
    assert len(store.recorded) == 1


def test_orchestrator_isolates_a_dispatch_failure_and_keeps_going():
    boom = _promo("2026.07.22.470", failure_reason="dev_dispatch_failed")
    ok = _promo("2026.07.22.474", failure_reason="dev_rollout_failed")
    store = _FakeStore(
        [boom, ok],
        {
            boom.release_version: [_event(boom.release_version, action="dev_dispatch_failed",
                                          created_at=_ago(minutes=30))],
            ok.release_version: [_event(ok.release_version, action="dev_rollout_failed",
                                        created_at=_ago(minutes=30))],
        },
    )
    spy = _DispatchSpy(raises_for={boom.release_version})
    # Must not raise even though the first candidate's dispatch throws.
    _reclaim(store, spy)
    assert (ok.release_version, AUTO_RETRY_ACTOR) in spy.calls
    assert len(spy.calls) == 2


def test_orchestrator_skips_a_candidate_superseded_by_a_newer_verified_release():
    verified = _promo("2026.07.22.500", state="dev_verified")
    old = _promo("2026.07.22.486", failure_reason="dev_rollout_failed")     # < 500 → superseded
    fresh = _promo("2026.07.22.511", failure_reason="dev_rollout_failed")   # > 500 → still relevant
    store = _FakeStore(
        [verified, old, fresh],
        {
            old.release_version: [_event(old.release_version, action="dev_rollout_failed",
                                         created_at=_ago(minutes=30))],
            fresh.release_version: [_event(fresh.release_version, action="dev_rollout_failed",
                                           created_at=_ago(minutes=30))],
        },
    )
    spy = _DispatchSpy()
    decisions = _reclaim(store, spy)
    # Only the newest candidate is re-dispatched; the superseded one is left alone.
    assert spy.calls == [(fresh.release_version, AUTO_RETRY_ACTOR)]
    superseded = [d for d in decisions if d.version == old.release_version]
    assert superseded and superseded[0].action == "skip" and superseded[0].reason == "superseded"


def test_orchestrator_does_not_raise_give_up_noise_on_a_superseded_exhausted_build():
    verified = _promo("2026.07.22.500", state="dev_verified")
    old = _promo("2026.07.22.486", failure_reason="dev_rollout_failed")
    events = {old.release_version: [_auto_dispatch(old.release_version, created_at=_ago(hours=6 - i))
                                    for i in range(5)]}
    store = _FakeStore([verified, old], events)
    spy = _DispatchSpy()
    _reclaim(store, spy, max_attempts=5)
    # Exhausted, but superseded → no give-up marker (the pipeline already moved past it).
    assert store.recorded == []
    assert spy.calls == []


# --- Real memory store: non-transition event append --------------------------

def test_memory_store_appends_a_non_transition_event():
    store = MemoryControlPlaneStore()
    store.create_release_candidate(
        _release_manifest("2026.07.22.501"),
        ReleasePromotion(release_version="2026.07.22.501", state="dev_failed",
                         failure_reason="dev_rollout_failed"),
        ReleasePromotionEvent(id="", release_version="2026.07.22.501",
                              action="dev_candidate_created", to_state="dev_pending"),
    )
    event = store.record_release_promotion_event(
        "2026.07.22.501", action=AUTO_RETRY_EXHAUSTED_ACTION, actor=AUTO_RETRY_ACTOR,
        note="dev_rollout_failed", metadata={"attempts": 5},
    )
    assert event.action == AUTO_RETRY_EXHAUSTED_ACTION
    assert event.from_state == "dev_failed" and event.to_state == "dev_failed"
    stored = store.list_release_promotion_events("2026.07.22.501")
    assert stored[-1].action == AUTO_RETRY_EXHAUSTED_ACTION
    assert stored[-1].metadata == {"attempts": 5}


def _release_manifest(version):
    from app.controlplane.base import ReleaseManifest
    return ReleaseManifest(version=version, git_sha="a" * 40, modules={"onebrain-api": version})

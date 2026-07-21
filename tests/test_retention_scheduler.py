"""The retention sweep is actually enqueued, at most daily, and only when asked.

`JOB_RETENTION_RUN` was defined and dispatched but enqueued by nothing, so a
configured retention policy was recorded and never enforced. These cover the
enqueue path and the three properties that make a scheduled destructive sweep
safe to run on every replica.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.deps
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore
from app.jobs.base import JOB_RETENTION_RUN
from app.jobs.memory import MemoryJobStore
from app.platform.base import Account, RetentionPolicy
from app.platform.memory import MemoryPlatformStore
from app.provisioning.customer_bootstrap import (
    CustomerBootstrapDescriptor,
    encode_customer_bootstrap,
)
from app.retention.scheduler import (
    start_retention_scheduler,
    sweep_idempotency_key,
    sweep_once,
)
from app.workers.service import Worker


def _platform(*account_ids: str) -> MemoryPlatformStore:
    platform = MemoryPlatformStore()
    for account_id in account_ids:
        platform.create_account(Account(id=account_id, kind="organization", name=account_id))
    return platform


def _policy(account_id: str, *, status: str = "active", domain: str = "intake") -> RetentionPolicy:
    return RetentionPolicy(
        id=f"ret_{account_id}_{domain}",
        account_id=account_id,
        space_id="",
        domain=domain,
        record_type="message",
        action="delete",
        duration_days=0,
        legal_basis="test policy",
        status=status,
    )


def test_sweep_enqueues_only_for_accounts_holding_an_active_policy():
    platform = _platform("with_policy", "no_policy", "inactive_policy")
    platform.upsert_retention_policy(_policy("with_policy"))
    platform.upsert_retention_policy(_policy("inactive_policy", status="archived"))
    jobs = MemoryJobStore()

    enqueued = sweep_once(platform, jobs)

    assert len(enqueued) == 1
    job = jobs.get(enqueued[0])
    assert job.type == JOB_RETENTION_RUN
    assert job.account_id == "with_policy"
    # Whole-account scope, the same one an operator-triggered sweep uses.
    assert job.space_id == ""
    # A counting-only sweep would leave the policy exactly as unenforced.
    assert job.payload["dry_run"] is False


def test_sweep_is_idempotent_per_account_per_utc_day():
    """Restarts, a short interval, and a second replica must not stack sweeps."""
    platform = _platform("acme")
    platform.upsert_retention_policy(_policy("acme"))
    jobs = MemoryJobStore()

    monday = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    later_that_day = datetime(2026, 7, 20, 21, 30, tzinfo=timezone.utc)
    tuesday = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)

    first = sweep_once(platform, jobs, now=monday)
    again = sweep_once(platform, jobs, now=later_that_day)
    next_day = sweep_once(platform, jobs, now=tuesday)

    assert first == again                      # same key -> the existing job
    assert next_day != first                   # a new day is a new sweep
    assert len({first[0], next_day[0]}) == 2
    assert sweep_idempotency_key("acme", monday) == "retention-sweep:acme:2026-07-20"


def test_one_unreadable_account_does_not_stop_the_others():
    platform = _platform("healthy", "broken")
    platform.upsert_retention_policy(_policy("healthy"))
    original = platform.list_retention_policies

    def explode(account_id: str, space_id: str = ""):
        if account_id == "broken":
            raise RuntimeError("policy store unavailable")
        return original(account_id, space_id)

    platform.list_retention_policies = explode
    jobs = MemoryJobStore()

    enqueued = sweep_once(platform, jobs)

    assert len(enqueued) == 1
    assert jobs.get(enqueued[0]).account_id == "healthy"


def test_a_box_that_cannot_enumerate_accounts_still_sweeps_its_own(monkeypatch):
    """The customer-box case, which is the one that matters.

    A box never receives the owner connection, so the operator DSN falls back to
    the application DSN and `platform_accounts` -- FORCED RLS, matching on
    `id = current_setting('app.account_id', true)` -- returns nothing to an
    unscoped read. Enumerating would sweep nothing on exactly the deployments
    holding customer data. The box names its own account instead.
    """
    platform = _platform("acme")
    platform.upsert_retention_policy(_policy("acme"))
    monkeypatch.setattr(platform, "list_accounts", lambda: [])   # RLS-blind, as on a box
    jobs = MemoryJobStore()
    settings = SimpleNamespace(customer_bootstrap=encode_customer_bootstrap(
        CustomerBootstrapDescriptor(account_id="acme", account_kind="organization",
                                    customer_name="Acme"),
    ))

    enqueued = sweep_once(platform, jobs, settings=settings)

    assert len(enqueued) == 1
    assert jobs.get(enqueued[0]).account_id == "acme"


def test_resolving_no_account_is_reported_not_silently_treated_as_no_work(caplog):
    platform = _platform()
    jobs = MemoryJobStore()

    with caplog.at_level("WARNING"):
        assert sweep_once(platform, jobs, settings=SimpleNamespace(customer_bootstrap="")) == []

    assert "resolved no account" in caplog.text


def test_scheduler_is_off_by_default_and_never_runs_on_mission_control():
    # A deploy must not start deleting customer records on its own.
    assert start_retention_scheduler(
        SimpleNamespace(operator_mode=False, retention_sweep_seconds=0)) is False
    # Mission Control stores no customer content to sweep.
    assert start_retention_scheduler(
        SimpleNamespace(operator_mode=True, retention_sweep_seconds=3600)) is False


def test_scheduled_sweep_actually_deletes_an_aged_record(monkeypatch):
    """End to end: the enqueued job is the one the worker knows how to run."""
    platform = _platform("nft_gym")
    platform.upsert_retention_policy(_policy("nft_gym"))
    intake = MemoryIntakeStore()
    # Age the record explicitly. Retention compares created_at against a cutoff
    # of now - duration_days, so a record written by the pipeline in this same
    # instant is not provably older than a duration_days=0 cutoff and the test
    # would race the clock.
    intake.create(IntakeRecord(
        id="rec_aged",
        tenant_id="nft_gym",
        account_id="nft_gym",
        space_id="sp_customer",
        app_id="communication",
        purpose="customer_service_inbox",
        source="communication",
        source_ref="msg-1",
        record_type="message",
        intent="booking",
        classification="internal",
        confidence=1.0,
        status="approved",
        title="Aged customer message",
        content="Aged customer message.",
        summary="Aged.",
        created_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    ))
    monkeypatch.setattr(app.deps, "get_platform_store", lambda: platform)
    monkeypatch.setattr(app.deps, "get_intake_store", lambda: intake)
    jobs = MemoryJobStore()
    assert intake.count() == 1

    enqueued = sweep_once(platform, jobs)
    Worker(jobs, worker_id="worker_test").run_once()

    assert jobs.get(enqueued[0]).status == "succeeded"
    assert intake.count() == 0

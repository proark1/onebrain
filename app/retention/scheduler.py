"""Retention scheduler — enqueue the sweep that nothing was enqueueing.

`JOB_RETENTION_RUN` has always been defined (`app/jobs/base.py`) and dispatched
(`app/jobs/handlers.py`), and `run_retention` has always worked. Nothing in the
application ever created the job, so a configured retention policy was recorded,
displayed, and never enforced: records aged past `duration_days` were kept
indefinitely. This closes that gap.

`sweep_once` is one tick and is pure of scheduling, so it is unit-testable.
`start_retention_scheduler` runs it on a daemon thread, mirroring
`app/fleet/retention.py:start_fleet_retention` and
`app/controlplane/reconcile_scheduler.py:start_reconcile_scheduler`.

Three properties this file exists to guarantee:

OFF BY DEFAULT. `retention_sweep_seconds` defaults to 0. Retention deletes
customer records, so it may not switch itself on: landing this package enforces
nothing until an operator sets a positive interval. This mirrors the reconcile
scheduler's opt-in rule for the same reason.

AT MOST ONE SWEEP PER ACCOUNT PER UTC DAY. Every job carries the idempotency key
`retention-sweep:<account>:<YYYY-MM-DD>`, and `JobStore.enqueue` returns the
existing job for a repeated key instead of creating another. A restart loop, a
short interval, and a second API replica therefore cannot stack concurrent
destructive sweeps over one account — which is what makes it safe to run this
daemon on every replica without leader election.

CUSTOMER-SIDE ONLY. Mission Control holds deployment metadata, not customer
content, so it has nothing to sweep and never starts the daemon.

The sweep is deliberately whole-account (`space_id=""`, the same scope an
operator-triggered sweep uses). Per-space jobs would multiply the job count by
the space count for no added coverage, since `run_retention` already resolves
each policy's own scope.

WHY THE ACCOUNT SET IS NOT `list_accounts()`. That is a cross-account operator
read: it connects on the operator DSN, which bypasses RLS by identity. A
customer box is never given the owner connection (`render.py`: "Customer API
containers never receive the owner connection"), so the operator DSN falls back
to the application DSN, `platform_accounts` has FORCED row-level security with
`id = current_setting('app.account_id', true)`, and an unscoped read matches
nothing. Enumerating would therefore return zero accounts on precisely the
deployments that hold customer data — sweeping nothing, forever, with no error.
So the box names its own account from its bootstrap descriptor, which is exactly
the scope every downstream read is already scoped to. `list_accounts()` remains
the fallback for stacks that legitimately see every account (Mission Control,
local, and test stores), and resolving to an empty set is logged rather than
treated as "nothing to do".
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.jobs.base import JOB_RETENTION_RUN

_log = logging.getLogger("onebrain.retention")

REQUESTED_BY = "system:retention-scheduler"
# A tiny floor: retention is a daily-cadence concern, and a fat-fingered tiny
# interval must not spin the account scan. The idempotency key already bounds
# the destructive work itself to once per account per day.
MINIMUM_INTERVAL_SECONDS = 300


def sweep_idempotency_key(account_id: str, now: datetime) -> str:
    return f"retention-sweep:{account_id}:{now.astimezone(timezone.utc).date().isoformat()}"


def target_account_ids(settings, platform) -> list[str]:
    """Resolve the accounts this deployment may sweep.

    A provisioned box names its own account, because it cannot enumerate (see
    the module docstring). Everything else falls back to the cross-account read.
    """
    from app.provisioning.customer_bootstrap import decode_customer_bootstrap

    try:
        descriptor = decode_customer_bootstrap(getattr(settings, "customer_bootstrap", "") or "")
    except Exception as exc:  # a malformed descriptor must not stop the fallback
        _log.warning("Retention sweep could not read the bootstrap descriptor: %s", exc)
        descriptor = None
    if descriptor and descriptor.account_id:
        return [descriptor.account_id]
    return [account.id for account in platform.list_accounts()]


def sweep_once(platform, jobs, *, settings=None, now: datetime | None = None) -> list[str]:
    """Enqueue a whole-account retention sweep for every account holding a policy.

    Returns the enqueued job ids. Accounts with no active retention policy are
    skipped entirely rather than enqueuing a no-op job, so an account that has
    never configured retention generates no queue traffic.
    """
    stamp = now or datetime.now(timezone.utc)
    account_ids = target_account_ids(settings, platform) if settings is not None else [
        account.id for account in platform.list_accounts()
    ]
    if not account_ids:
        # Distinguish "this deployment can see no account" from "no policy is
        # configured". The first is a misconfiguration that would otherwise look
        # exactly like a quiet, healthy no-op.
        _log.warning("Retention sweep resolved no account to sweep; nothing was enqueued.")
        return []
    enqueued: list[str] = []
    for account_id in account_ids:
        try:
            policies = platform.list_retention_policies(account_id)
        except Exception as exc:  # one unreadable account must not stop the rest
            _log.warning("Retention sweep skipped for %s: %s", account_id, exc)
            continue
        if not any(policy.status == "active" for policy in policies):
            continue
        try:
            job = jobs.enqueue(
                type=JOB_RETENTION_RUN,
                tenant_id=account_id,
                account_id=account_id,
                requested_by=REQUESTED_BY,
                # dry_run=False is the point: a scheduled sweep that only counted
                # records would leave the policy exactly as unenforced as before.
                # Legal holds, the per-record age filter, and the `retention_runs`
                # ledger are what make it safe, and run_retention applies all three.
                payload={"dry_run": False},
                idempotency_key=sweep_idempotency_key(account_id, stamp),
            )
        except Exception as exc:  # a queue failure is a skipped tick, never a crash
            _log.warning("Retention sweep could not be enqueued for %s: %s", account_id, exc)
            continue
        enqueued.append(job.id)
    return enqueued


def start_retention_scheduler(settings) -> bool:
    """Daemon: enqueue retention sweeps every `retention_sweep_seconds`.

    Returns False (no thread) on Mission Control, or unless an operator has
    explicitly set a positive interval. Never fatal — a failing tick is logged
    and the next one still runs.
    """
    if getattr(settings, "operator_mode", False):
        return False
    interval = int(getattr(settings, "retention_sweep_seconds", 0) or 0)
    if interval <= 0:
        return False
    interval = max(MINIMUM_INTERVAL_SECONDS, interval)

    def _loop() -> None:
        from app.deps import get_job_store, get_platform_store

        while True:
            try:
                enqueued = sweep_once(get_platform_store(), get_job_store(), settings=settings)
                if enqueued:
                    _log.info("Enqueued %s retention sweep(s).", len(enqueued))
            except Exception as exc:  # pragma: no cover - defensive (store getters)
                _log.warning("Retention sweep tick failed: %s", exc)
            time.sleep(interval)

    threading.Thread(target=_loop, name="retention-sweep", daemon=True).start()
    _log.info("Retention sweep scheduler started (every %ss).", interval)
    return True

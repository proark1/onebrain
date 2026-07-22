# OneBrain release-pipeline automation roadmap

**Date:** 2026-07-22
**Author:** operator + Claude (session 2775207f)
**Status:** planning — no code yet

## Why this exists

Bringing PR #55 live tonight was mostly manual: hand-diagnose a bug, hand-deploy
Mission Control, hand-prune a full disk, hand-retry a stranded candidate. The goal
of this doc is to separate the **one-time** manual work from the **systemic** gaps,
map every operator-toil point to the code that causes it, and sequence the fixes —
before writing any of them.

The guiding rule: **automate the recovery paths, not the security firebreaks.**

---

## What is already automated (baseline — don't rebuild)

| Capability | Mechanism |
|---|---|
| Fleet boxes self-update | `onebrain-update.timer` → `update.sh` pulls `/api/fleet/desired-state`, applies, prunes after a verified update (#37) |
| Fleet boxes self-maintain | `onebrain-host-maintenance.timer` (daily), `onebrain-drive-backup.timer` (daily 02:30 UTC) |
| Control-plane reconcile loop | `start_reconcile_scheduler` runs `reconcile_once` every `fleet_reconcile_seconds` on MC (opt-in, ≥30s floor) → `reconcile_pull_targets` |
| Dev-candidate dispatch | on gate heartbeat, `dispatch_waiting_development_candidate` sends the oldest `dev_pending` to the gate ([fleet.py:151](../../../app/routers/fleet.py)) |
| MC self-deploy (compute side) | `dispatch_operator_self_rollout` opens a forward-only rollout to the newest `dev_verified` tip, trusting the CI **development** signature for MC's own box ([operator.py:2132](../../../app/routers/operator.py)) |
| Customer delivery (post-approval) | once `customer_approved` + production-signed, boxes pull + apply on their own timer |

## Intentionally manual — do NOT automate (security firebreak)

- **Production signing** — the offline/air-gapped key.
- **Customer approval gate** — a human decides what reaches customers.

These two are the whole point of the trust model. Automating them defeats it. Every
other manual step below is a gap, not a feature.

---

## The real gaps (each mapped to its cause)

### Gap A — Dev candidates never auto-retry  ★ highest leverage
- **Toil tonight:** the whole 471–501 backlog sat at `dev_failed` after the gate came
  back; I hand-ran `retry-dev` on 486.
- **Cause:** `dispatch_waiting_development_candidate` selects **only** `dev_pending`
  ([operator.py:2105-2108](../../../app/routers/operator.py)). Nothing ever moves a
  `dev_failed` candidate back to retryable. A failure is terminal-until-a-human.
- **Fix:** a bounded self-healing retry on the reconcile tick / heartbeat — reclassify
  `dev_failed` candidates whose `failure_reason` is **transient** back into the dispatch
  path, with a per-candidate attempt cap + backoff, and an alert when it gives up.
- **Payoff:** tonight's re-binding bug would have **self-healed silently** (first auto-retry
  re-binds, second passes) — no page, no investigation.
- **Effort:** M · **Risk:** M (retry storms / masking a genuinely bad build → mitigated by
  transient classification + caps + give-up alert).

### Gap B — MC self-update is computed but never applied
- **Toil tonight:** hand-edited `images.override.yml` + `docker compose --force-recreate`
  on the MC root box.
- **Cause:** `dispatch_operator_self_rollout` opens the rollout and `reconcile_pull_targets`
  serves MC its desired-state, but **MC's host runs none of the box agents** — it is a
  hand-run `docker compose` deployment at `/opt/onebrain` (project `onebrain-mc`), not a
  broker box with `onebrain-update.timer`/`update.sh`. So the rollout is opened and never
  consumed. Compounding it, the self-deploy **"never re-attempts a failed target"**
  ([operator.py:2163-2167](../../../app/routers/operator.py)), so a *transient* apply
  failure (e.g. tonight's disk-full) would permanently skip that release.
- **Fix:** give MC a box-side applier — either run the fleet update agent against MC's own
  served desired-state, or a small MC-specific applier on a timer: *prune → pull digests →
  compose recreate → health-check → report → roll back on failure*. Relax the never-retry
  guard for transient failures.
- **Effort:** M–L · **Risk:** M (MC is the control plane; a bad self-apply must fail safe and
  roll back to the previous digest set).

### Gap C — MC has no host hygiene (disk)
- **Toil tonight:** MC hit **100% disk** (36 GB of hoarded images) and the recreate failed
  mid-pull; freed 30 GB by hand with `docker image prune -a -f --filter until=1h`.
- **Cause:** MC runs neither `onebrain-host-maintenance.timer` nor the post-update prune;
  every manual deploy leaves a full api+admin-ui+workers trio (~3.6 GB) behind forever.
- **Fix:** a prune timer on MC (**already queued** as task `task_07347912`), and fold the
  same prune into the Gap-B applier so a deploy can never wedge on disk.
- **Effort:** S · **Risk:** low.

### Gap D — No stall / health alerting
- **Toil across sessions:** every incident (gate disk-full, promotion deadlock, this
  re-binding bug, MC disk-full) was **discovered by a human days later**, not signalled.
- **Cause:** no alerting on pipeline or infra health.
- **Fix:** alert on — `dev_failed` backlog age > threshold, gate/MC heartbeat stale, disk
  pressure on MC/gate, and self-deploy give-up (from Gap A/B). Channel TBD.
- **Effort:** M · **Risk:** low.

### Gap E — Gate lifecycle
- **E1 — fresh-gate migration bootstrap — DONE (#57, 2026-07-22).** A fresh gate could not
  verify *any* migration-crossing release until its first 02:30 backup existed (why 501/511 were
  parked). Fixed by exempting `is_release_gate` from the **pre-dispatch** backup gate — the gate
  is disposable and `update.sh` still takes its own inline pre-migration `pg_dump`; customer
  boxes and MC self-update stay fully gated. **Caveat:** the planner runs on MC, so this only
  takes effect once MC runs a release ≥ #57 — which today means another manual MC deploy (Gap B)
  or the 02:30 backup unblocking it first. A live example of why Gap B matters.
- **E2 — gate auto-replacement — remaining.** Replacing a gate is still fully manual.
  - *Fix (later):* auto-provision a replacement gate on sustained unhealthy/disk-full.
  - *Effort:* L · *Risk:* M–H (auto-provisioning infrastructure).

---

## Recommended sequence

1. **Phase 1 — Self-healing dev pipeline.** Gap A + a give-up alert (Gap D-lite). Biggest
   toil reduction per unit effort; directly prevents the "notice days later, hand-retry"
   loop.
2. **Phase 2 — MC runs itself.** Gap B + Gap C together (applier + prune). Ends the
   hand-deploy that dominated tonight.
3. **Phase 3 — Observability.** Gap D in full.
4. **Phase 4 — Infra self-healing.** Gap E2 (gate auto-replacement). E1 already landed (#57).

Each phase ships as its own PR per `AGENTS.md`. Phases 1–3 are pure control-plane / host
work with no change to the trust model.

## Design forks to decide before Phase 1

1. **Transient vs permanent classification** — *the crux.* Which `failure_reason`s auto-retry?
   - *Transient (retry):* `dev_rollout_failed`, `development_gate_target_unavailable`,
     `backup_required_for_schema_update` (once a backup exists), secrets-epoch-pending.
   - *Permanent (do not retry — needs a human/new build):* `release_missing_modules`,
     `release_signature_invalid`, module-set-invalid, `release_yanked`.
2. **Retry policy** — max attempts per candidate, backoff curve, and whether to retry the
   whole backlog or only the newest N (recommend: newest only — old builds are superseded).
3. **MC self-apply autonomy** — fully automatic (trust the dev signature, already the design
   intent) vs. require a one-click operator ack per MC update.
4. **Alert channel + owner** — email / operator console / webhook, and who is on the hook.

## Out of scope

Production signing and customer approval stay manual — by design.

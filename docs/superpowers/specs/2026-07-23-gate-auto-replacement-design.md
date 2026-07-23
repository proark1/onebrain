# Gate auto-replacement — design pass (roadmap Phase 4, Gap E2)

**Date:** 2026-07-23
**Author:** operator + Claude (session 2775207f)
**Status:** design-only — no code yet. One fork (autonomy tier) is the operator's call.

## Why this exists

Roadmap Gap E2: "auto-provision a replacement gate on sustained unhealthy/disk-full."
Today replacing the development release gate is **fully manual**, and a gate dying
(the 2026-07-22 disk-full incident) stalls the whole release pipeline until a human
notices and rebuilds it. This is the roadmap's last and **riskiest** piece: it is the
only automation that *provisions* — and potentially *destroys* — real, billable
infrastructure. So this is a design pass before any code.

## What already exists (the building blocks)

Manual gate replacement is already two operator endpoints an auto-replacer would just
*sequence*, plus a surprising amount of replacement scaffolding:

| Piece | Where | Note |
|---|---|---|
| Provision a (replacement) gate | `POST /api/operator/development-gate/provision` → `operator.py:2533` | synchronous; funnels to the broker |
| Suffixed replacement identity + one-in-flight refusal | `_development_gate_identity` `operator.py:2359-2393` | refuses a *second live undesignated* gate (`:2384-2385`) |
| Dead-row guard (#54) | `_is_live_gate_replacement` `operator.py:2330-2356` | a terminally-failed provision doesn't wedge the next |
| Designate the gate | `PUT /api/operator/development-gate/{id}` → `operator.py:2508` | atomic, **strictly one gate** (`postgres.py:550`) |
| Pre-designation health/shape checks | `_development_gate_blockers` `operator.py:2258-2319` | fresh healthy heartbeat + module set + trusted baseline |
| Replacement baseline/seed trust chain | `operator.py:1708-1770`, `1797-1873`; `development_gate.py:35-63` | lets a replacement keep trusting the dev-signed seed |
| Health telemetry on the row | `base.py:114-118` (`last_heartbeat_at`, `last_heartbeat_healthy`, …) | written every heartbeat |
| Duration-thresholded detection precedent | `app/fleet/pipeline_watchdog.py:99-123` (`stall_seconds`) | the pattern the trigger reuses |
| Teardown the old gate | create→approve→execute, `operator.py:1282-1625`; broker `/v1/destroy` | dual-control + P1-D guard |

**Nothing auto-provisions today** — every box is operator-triggered. The reconcile and
watchdog daemons observe and alert; they never create or destroy infrastructure.

## Safety invariants this design must not weaken

From the broker/teardown map — these are load-bearing and non-negotiable:

1. **P1-D destroy guard** (`broker.py:215-239`): destroy scope is derived from
   `deployment_id` + `managed-by=onebrain-fleet` labels; MC passes **only a deployment id**;
   no primitive both un-protects and deletes. Any auto-teardown keeps calling
   `destroy_box(<old_gate_id>, confirm=True)` — never a resource-id path.
2. **Server cap = 5** (`broker.py:165-175`, `hetzner_max_fleet_servers`, `config.py:424`):
   the **only** hard backstop against provisioning runaway. Do not bypass `provision_box`;
   do not raise the cap blindly.
3. **Dual-control, fail-closed** (`base.py:472-491`) + execute-time re-validation
   (`operator.py:1510-1511`): unattended auto-teardown effectively requires
   `min_approvals=1` + self-approval — the *accepted residual risk*. Keep it explicit,
   greppable, and defaulted-strict.
4. **Token isolation / broker-only** (`broker.py:297-337`): MC must never hold the Hetzner
   token; go through the remote broker, no in-process fallback.
5. **Live-gate + tombstone guards** (`operator.py:1518-1521`; `postgres.py:531,136`): never
   decommission the currently-designated gate or a tombstoned row. **Ordering is fixed:**
   provision → verify healthy → **designate replacement** → only then teardown the old
   (now-undesignated) gate.

## The failure modes to design against (not the happy path)

- **Cost runaway — the #1 risk.** There is **no provisioning rate-limit / debounce anywhere**
  (`config.py:421-423` says so explicitly). Each replacement gets a *distinct* deployment id,
  so broker idempotency (keyed on deployment id) does **not** collapse re-fired replacements —
  the cap of 5 is the only ceiling. A flapping gate + a naive trigger = mint boxes until the
  cap. **Mitigation is mandatory, not optional:** sustained-ness (debounce) + one-in-flight +
  a min-interval rate cap, with the cap as final backstop.
- **Orphan-wedge.** If provision succeeds but designation never completes (new gate never goes
  healthy / no candidate verifies), the replacement is *detected as live* and **blocks further
  provisions** (`operator.py:2384-2385`) while the old gate stays designated-but-dead — the
  pipeline is stuck and a cap slot is consumed. The auto-replacer must **degrade to an alert
  and stop**, never loop.
- **Stuck backlog after replacement.** Designation does not migrate promotions;
  `dev_failed` candidates stay bound to the dead gate until re-dispatched — which is exactly
  what Phase 1 auto-retry (#61) + the #55 re-bind do. So **auto-replacement composes with #61**;
  without #61 enabled, a human still runs `retry-dev` after a replacement.

## The sequence (idempotent, world-derived state — no separate FSM)

Each tick on Mission Control (a new opt-in daemon, mirroring the watchdog/reconcile
schedulers — MC-only, off by default):

1. **Detect** — the designated gate (`get_release_gate`) has a sustained failure signal:
   a `missed_heartbeat` (dead box) or disk signal (`low_root_disk` / `data_volume_unavailable`)
   whose `FleetAlert.created_at` age exceeds `gate_replace_after_seconds`. (unhealthy-only is
   softer — see fork B.)
2. **Guard** — all must hold, else no-op this tick: no live replacement already exists
   (reuses `_development_gate_identity`'s refusal); last replacement was > `min_interval` ago;
   fleet has cap headroom for +1; MC is production-ready (`assert_production_mission_control_ready`).
3. **Provision** — call the same underlying provision path the endpoint uses (not the HTTP
   endpoint), supplying a fixed daemon principal + the gate's `owner_email` (the provision path
   *requires* an owner email and rejects an external Hetzner provision without one) → a suffixed
   replacement box + provisioning run.
4. **Wait** — the replacement boots, enrolls, and heartbeats healthy, and passes
   `_development_gate_blockers` (shape / module set / fresh healthy heartbeat / trusted baseline).
   A `replace_timeout` without a healthy, enrolled box → **alert + stop** (orphan-wedge guard).
   Note: do NOT wait for a `dev_verified` candidate here — dev-dispatch targets the *currently
   designated* gate (`store.get_release_gate()`), so a fresh replacement cannot verify anything
   until it is already the gate. Designation, not verification, is the gate promotion.
5. **Designate** — `designate_release_gate(replacement)` (the same preflight the manual
   `PUT …/development-gate/{id}` runs). This is what makes it the gate: only *after* designation
   does dev-dispatch target it, so #55 + #61 re-bind the stalled backlog onto the new gate and it
   verifies the tip. (Provisioning from a trusted baseline + the blocker preflight are what make
   designating an as-yet-unverified fresh gate safe — exactly the manual flow's contract.)
6. **Old-gate disposition** — *tier-dependent* (fork A): alert the operator to reap it, or
   auto-reap after a grace period.

## The forks (decisions)

### Fork A — autonomy tier (THE decision)

- **Tier 1 — detect + alert.** A new `gate_replacement_recommended` alert (delivered via the
  Gap-D webhook shipped in **#65** — so this tier depends on #65 landing, not on a hypothetical
  channel) fires when the gate is sustained-dead. The operator runs the existing two-endpoint
  replace. *Effort S · Risk low.* Closes most of "found out days later" for gate death with
  almost no new surface. **Build this regardless.**
- **Tier 2 — auto-provision + designate (steps 1-5), teardown stays manual/alerted.** Zero-touch
  pipeline recovery; the destructive step (teardown) stays human, prompted by a "old gate ready
  to decommission" alert. *Effort M-L · Risk M* — the cost-runaway rails are the hard part.
  This is the roadmap's actual Gap E2 goal.
- **Tier 3 — full end-to-end incl. auto-teardown.** Requires a **standing** `min_approvals=1`
  + self-approval so the daemon can execute a teardown unattended — and because
  `_teardown_dual_control_policy` reads the **global** `teardown_min_approvals` /
  `teardown_allow_self_approval` (not a per-account setting), that relaxation lowers the
  dual-control bar for **every** teardown, customer boxes included. *Effort L · Risk H.*
  **Recommended against** — it removes the human from an irreversible, billable destroy *and*
  globally weakens the teardown gate, to save one operator action on a rare event. Keep teardown human.

### Fork B — trigger criteria + debounce/caps (settle if Tier ≥ 2)

- Which signals fire it: `missed_heartbeat` (dead box) + `low_root_disk`/`data_volume_unavailable`
  (disk death, the 2026-07-22 case) are the strong ones. `unhealthy` alone is softer (an app
  restart self-recovers) — either exclude it or give it a longer threshold.
- `gate_replace_after_seconds` (sustained window) — proposed default 1800 (30 min).
- Cost caps: `gate_replace_min_interval_seconds` (proposed 6h), one-in-flight, cap=5 backstop.

### Fork C — old-gate disposition (only if Tier 3)

Auto-reap immediately after the new gate is proven, vs. after a grace period. Moot under
Tiers 1-2 (operator reaps, prompted).

## Recommendation

**Tier 1 now** (safe, tiny, immediate value; a natural extension of the Gap-D alerting just
shipped), and treat **Tier 2 as the deliberate opt-in** that delivers the roadmap's goal —
built as its own PR with the debounce/one-in-flight/min-interval rails front-and-centre and an
opt-in `gate_auto_replace_enabled` flag defaulted off. That flag (like the other new MC
settings) must be threaded through the `box.env` / bootstrap render path, or a provisioned MC
cannot set it — folded into the same config-threading follow-up tracked for #63/#65. **Do not
build Tier 3**; keep the irreversible teardown a human action, prompted by an alert.

Rationale: gate death is rare and the manual replace is cheap, so the marginal value of full
autonomy is small, while the cost-runaway and irreversible-destroy risks are real and
code-confirmed. Tier 1 captures most of the value at almost no risk; Tier 2 is worth it only if
the alert-then-manual loop proves annoying enough to justify the rails.

## Out of scope

The broker's guards (P1-D, server cap, token isolation, dual-control) are not touched — they are
the invariants this rides on. Production signing and customer approval remain manual, as always.

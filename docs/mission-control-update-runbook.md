# Updating Mission Control

How a change merged to `main` reaches the running Mission Control host, and the
one place that path behaves differently from a customer box.

[Mission Control standup](mission-control-standup.md) covers standing the host
up. This covers moving it forward afterwards.

> **The short version.** Merging does not deploy. Images publish automatically
> and a candidate reaches the development gate automatically; everything past
> that is a person. Mission Control then needs a rollout opened against *itself*
> — it does not follow the fleet — and that rollout will apply successfully and
> then report a timeout, because MC cannot see its own update outcome. See
> [MC cannot confirm its own update](#mc-cannot-confirm-its-own-update).

## What happens without you

**On push to `refs/heads/main`** (a merged PR — not a tag, not a label):

1. **Images publish.** `.github/workflows/tests.yml:420` calls
   `publish-images.yml`, which builds `api`, `workers` and `admin-ui` and pushes
   them to `ghcr.io/proark1/…` tagged `sha-<full-git-sha>`, plus `:latest` on the
   default branch. Digests exist only as outputs *within that run* — they are not
   committed anywhere and cannot be retrieved cross-run
   (`publish-images.yml:1-4`). If step 2 does not run, you must re-resolve them
   by hand with `docker buildx imagetools inspect ghcr.io/proark1/<image>:sha-<sha>`.

2. **A development candidate registers.** `tests.yml:429` runs
   `scripts/register_release_candidate.py`, which POSTs the digest-pinned
   manifest to `POST /api/operator/release-candidates` on Mission Control and
   signs it with the **development** key. Version is derived from the clock and
   run number (`YYYY.MM.DD.<run>`), not from a tag.

   Gated on repo variable `ONEBRAIN_RELEASE_CANDIDATE_ENABLED`. Check it before
   assuming this ran: `gh variable list`.

3. **The candidate deploys to the development gate.** Registration creates a
   `ReleaseManifest` (`status="draft"`) and a `ReleasePromotion` at
   `dev_pending`, then immediately dispatches it to the designated gate
   (`app/routers/operator.py`, `_dispatch_development_candidate`) → `dev_deploying`.

4. **The gate verifies itself.** `dev_deploying` → `dev_verified` happens only
   when the gate's own heartbeat agrees on version, migration revision, attempt
   id, module set and secrets epoch (`app/controlplane/promotion.py`,
   `_heartbeat_failure_reason`). No human can assert this step; a stall becomes
   `dev_failed` on timeout.

Automation stops here. A `dev_verified` release has reached exactly one internal
box and no customer.

## What needs a person

5. **Sign offline.** `scripts/sign_release.py sign` is the only place the
   production release key is ever used, and it must run on an offline machine —
   that key never reaches Mission Control. Paste the signature into
   `POST /api/operator/releases/{version}/production-signature` (Control →
   Releases in the console). This attaches the signature and deliberately does
   **not** change promotion state.

6. **Approve.** `POST /api/operator/releases/{version}/approve` moves
   `dev_verified` → `customer_approved` and flips the manifest to
   `status="active"`. The signature is re-verified at this boundary.

   Approving still deploys nothing. It only makes the release selectable.

7. **Open a rollout.** `POST /api/operator/deployments/{id}/rollouts`, or the
   Control → Rollouts screen. Nothing moves any box without this.

## How a box actually picks the release up

Every box, Mission Control included, runs `onebrain-update.timer` —
`OnBootSec=2min`, then every 5 minutes. Each tick:

1. GETs `${ONEBRAIN_FLEET_URL}/api/fleet/desired-state` with its own fleet key.
   A box can only ever fetch its own state; the key is pinned to one deployment
   id and a mismatch is a 403.
2. Verifies the signed envelope with `onebrain_box_verify.py` — wrapper
   signature, release signature, deployment id, expiry, nonce replay, version
   floor, registry allowlist. **The verifier's stdout is the only source of image
   refs**; the raw envelope is never trusted.
3. Compares the verified *digest set* against what is running — not version
   strings. Equal digests and equal migration revision is a logged no-op.
4. Pulls the pinned images, takes an encrypted `pg_dump -Fc` backup if the
   release crosses a migration, runs the migration with services quiesced,
   restarts, then fences: if `alembic current` does not equal the target, it
   recovers (code-only rollback, or a `pg_restore` for a `restore_required`
   release).

If Mission Control is unreachable the box logs `mc unreachable; holding
last-known-good` and exits. A box never updates itself toward anything MC did
not sign.

**What makes a version "desired"** is only two things
(`app/controlplane/desired_state.py`, `target_release_for_deployment`): an
active, non-terminal rollout's `target_version`, or — with no rollout — the
deployment's own `current_version`, which is just a steady-state confirm.

That is why merging changes nothing on any box until step 7.

## Mission Control's own update

MC is a box like the others, with two differences that matter.

**It self-seeds as `manual`.** `app/controlplane/self_seed.py` registers the MC
deployment with `release_ring="manual"` and `update_policy="manual"`, so it is
excluded from fleet ring sweeps. It is *not* excluded from an explicit
single-deployment rollout — only `pinned` blocks that. So MC will never be swept
up by a fleet rollout, and will never move until you aim a rollout at it.

**It polls itself.** `ONEBRAIN_FLEET_URL` on the MC host is MC's own public URL,
so every 5 minutes the host curls its own API for its own desired state.

To move Mission Control forward, open a rollout against the MC deployment id
after the release is approved. Then read the warning below.

### MC cannot confirm its own update

The host updater writes its outcome to
`${ONEBRAIN_DATA_MOUNT}/onebrain-maintenance/onebrain_update/update_state.json`
(default `/mnt/onebrain-data/...`). Two different things read that file:

- On a **customer box**, `onebrain_gate_report.py` — a root-owned reporter that
  reads the maintenance path directly.
- On **Mission Control**, nothing. That reporter is installed only when
  `role != "operator"` (`app/provisioning/hetzner/render.py`), so MC falls back
  to the in-app reporter, which reads `<ONEBRAIN_DATA_DIR>/update_state.json` —
  i.e. `/data/update_state.json`. A different filesystem location.

Consequence: MC's heartbeat `UpdateReport` stays at its default
`outcome='none'` forever. `pull_acknowledgement_matches`
(`app/controlplane/pull_reconcile.py`) requires `outcome == "succeeded"`, so a
rollout opened against MC **can never be acknowledged**. It stays non-terminal
until it is synthesized as a timeout.

**The update itself still applies.** Only the acknowledgement is missing. So:

- Do **not** read a timed-out MC rollout as a failed update.
- Do **not** retry on the strength of that status alone.
- Verify by version instead, not by rollout state.

### Verifying an MC update actually landed

The `onebrain.version` in a heartbeat is reported by the running application,
not by the update state file, so it is accurate on MC.

- **Console:** Fleet → Overview, the Mission Control row, Release column. It
  should show the new version, with no "Registry expects …" mismatch.
- **API:** `GET /api/fleet/overview`, the MC row's `reported_version`.
- **On the host:** the update log written by `update.sh` under
  `${ONEBRAIN_MAINTENANCE_DIR}/onebrain_update/`. Mission Control is the one box
  reachable over SSH — broker-provisioned customer boxes are not.

Fixing the reporting gap properly means either installing the gate reporter on
the operator role, or pointing the in-app reporter at the maintenance path. Until
then, treat MC rollout status as unreliable and version as authoritative.

## Order of operations

```
merge to main
  └─ images publish to GHCR                      (automatic)
  └─ candidate registers + deploys to dev gate   (automatic, if enabled)
  └─ gate heartbeat verifies                     (automatic)
     └─ sign offline + attach signature          (person, offline key)
        └─ approve for customers                 (person)
           └─ open a rollout per deployment      (person, one at a time)
              └─ box pulls within ~5 min         (automatic)
```

Mission Control sits at the last step like any other box — but you must aim the
rollout at it, and you must verify it by version.

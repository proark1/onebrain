# Mission Control — standup runbook

How to bring the fleet control plane from "shipped but inert" to "operational".
Everything referenced here is already merged to `main` and deployed; this runbook
only sets configuration and infrastructure. It requires Railway access.

Mission Control is the **same OneBrain image** run in operator mode. It ingests
metadata-only heartbeats, serves the fleet overview, mints enrollment keys, and
dispatches fleet rollouts. It holds no customer content.

---

## 0. Prerequisites

- A Railway account/workspace (separate project from every customer stack).
- A strong `ONEBRAIN_AUTH_SECRET` (`python -c "import secrets;print(secrets.token_hex(32))"`).
- The public hostname the MC instance will have (e.g. `https://mission-control.up.railway.app`).

## 1. Stand up the Mission Control instance

Create a new Railway project + Postgres, deploy the OneBrain image with:

```
ONEBRAIN_OPERATOR_MODE=true          # ingest heartbeats + serve /api/fleet
ONEBRAIN_OPERATOR_CONSOLE=true        # serve the operator/provisioning surface here
ONEBRAIN_VECTOR_STORE=pgvector
ONEBRAIN_DATABASE_URL=<mc postgres dsn>
ONEBRAIN_MIGRATION_DATABASE_URL=<mc postgres owner dsn>
ONEBRAIN_AUTH_SECRET=<strong secret>
ONEBRAIN_ADMIN_EMAIL=<you@domain>     # the operator login
ONEBRAIN_ADMIN_PASSWORD=<strong>
ONEBRAIN_FLEET_PUBLIC_URL=https://mission-control.up.railway.app   # handed to enrolling deployments
ONEBRAIN_COOKIE_SECURE=true
```

First boot runs migrations (`alembic upgrade head` → 0018) and starts the API. It
migrates-before-start and fails closed, so a bad DSN refuses to start rather than
serve broken. Confirm `GET /health` → 200 and log in to the console; the **Fleet**
tab appears (it 404s the fleet API on any non-operator stack, by design).

> Deploy the `onebrain-web` console alongside MC (or point an existing console at
> the MC API). The Fleet tab reads `/api/fleet/*` and `/api/operator/fleet-rollouts`.

## 2. Register the existing deployments

Mission Control's registry starts empty. For each existing customer deployment,
register it (operator console → Control → deployments, or `POST /api/operator/deployments`):

```
POST /api/operator/deployments
{ "id": "dep_<account_id>", "customer_name": "...", "account_id": "<account_id>",
  "deployment_type": "dedicated_railway", "release_ring": "pilot",
  "current_version": "<running version>", "current_migration": "0018_fleet_rollouts" }
```

Use `dep_<account_id>` as the id (matches the provisioning convention). `account_id`
is now authoritative for authorization. Set `release_ring` deliberately — it decides
which fleet-rollout wave a deployment lands in (`manual` opts out of fleet sweeps).

## 3. Enroll each deployment (so it reports heartbeats)

On Mission Control, for each registered deployment:

```
POST /api/fleet/deployments/<deployment_id>/enroll
→ { "key_id": "...", "env": {
      "ONEBRAIN_FLEET_URL": "https://mission-control.up.railway.app",
      "ONEBRAIN_DEPLOYMENT_ID": "<deployment_id>",
      "ONEBRAIN_FLEET_KEY": "fk_..."   # shown ONCE
  }}
```

Set those three env vars on that customer deployment's `onebrain-api` service and
redeploy. Its reporter self-activates (it is a no-op until all three are present)
and begins POSTing a heartbeat every 60s. Within a minute the deployment appears
healthy in the Fleet overview. (New deployments provisioned after auto-enrollment
is wired get these injected automatically — see §6.)

## 4. Verify heartbeat ingest

- Fleet tab → Overview: the deployment shows `healthy`, a reported version, and a
  recent "last seen".
- `GET /api/fleet/deployments/<id>/history` returns the accumulating count series.
- Heartbeats are metadata-only (counts/flags/versions) — verify no customer content
  is present (`fleet.v1` is a closed schema, `extra="forbid"`).

## 5. Wire the rollout executor (to fire real updates)

The rollout dispatch + fleet rollouts need GitHub Actions + Railway config. On the
deployment that dispatches (Mission Control):

```
ONEBRAIN_GITHUB_OWNER / ONEBRAIN_GITHUB_REPO / ONEBRAIN_GITHUB_DISPATCH_TOKEN
ONEBRAIN_PROVISIONING_CALLBACK_KEY_ID / ONEBRAIN_PROVISIONING_CALLBACK_KEY_HASH
ONEBRAIN_SECRET_ENCRYPTION_KEY
ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS=mission-control.up.railway.app   # lock the callback host
```

GitHub repo secrets: `RAILWAY_TOKEN` (or `RAILWAY_API_TOKEN`),
`ONEBRAIN_PROVISIONING_CALLBACK_KEY` (must hash-match the server value).
`update-customer.yml` and `provision-customer.yml` are already on `main`.

**Always dry-run first.** A fleet rollout with `dry_run: true` exercises the full
dispatch → callback path with zero Railway changes and (by design) applies no
version — use it to confirm the plumbing before a real sweep.

## 6. Auto-enrollment for new customers (follow-up, not yet wired)

The enroll endpoint + `fleet_enrollment_vars` exist; the remaining step is to have
`CustomerProvisioner.provision` mint a fleet key and pass the three enrollment env
vars into `provision-customer.yml`'s `common_vars` (via the one-time-secret
envelope, not a plaintext dispatch input). Until then, run §3 manually for each new
deployment.

## 7. Watchdog + retention (operational)

- The missed-heartbeat / version-drift / unhealthy watchdog (`app/fleet/watchdog.py`)
  is pure logic; schedule `run_watchdog` on a timer on MC (a daemon-thread loop like
  the reporter, using `ONEBRAIN_FLEET_MISSED_HEARTBEAT_SECONDS`) plus an external
  dead-man ping on MC's own `/health` (a dead MC can't alert on itself).
- Schedule `FleetStore.prune_heartbeats` to enforce
  `ONEBRAIN_FLEET_HEARTBEAT_RETENTION_DAYS` (default 30) so `fleet_heartbeats` stays bounded.

## Rollback

All fleet migrations (0015–0018) are additive. The forward-only property applies:
a plain code revert past a migration fails closed — roll back with
`alembic downgrade <target>` (each migration has a clean downgrade) or roll forward.

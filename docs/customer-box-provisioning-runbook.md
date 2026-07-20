# Customer Box Provisioning and Recovery Runbook

This is the operator checklist for provisioning a Hetzner customer-shaped box
(the development gate, or a customer deployment) and for diagnosing one that
does not come up. It complements
[the production activation runbook](production-activation-runbook.md), which
covers the broker and credential boundary this procedure depends on.

Read section 6 before you start. Two independent one-hour clocks make a stalled
provision unrecoverable, and the box reports several failures silently.

## 1. Where operator API calls are made

Mission Control's session cookie (`ob_session`) is `HttpOnly`, so it cannot be
copied into `curl`. There is no CSRF token and no CORS middleware. Issue
operator calls from the **browser developer console on `mc.onlyonebrain.com`**
while signed in as the super admin; a same-origin `fetch` sends the cookie
automatically.

Chrome blocks the first paste into its console. Type `allow pasting` and press
Enter once per profile.

Do not run these snippets in an SSH session. They are JavaScript, not shell.

## 2. Prerequisites

Verify all of these before provisioning. Each one produces a distinct failure:

- The broker is reachable and `provisioner_backend` is `hetzner`. Otherwise the
  provision returns `409 Development gate provisioning requires the Hetzner
  backend`.
- An approved release covers **every** module in the deployment's composition.
  A full-stack box needs all eight modules of `MODULE_IDS`, not only the three
  Core images. Missing coverage returns `409 Baseline release does not cover
  the development gate modules: ...`.
- The release's `onebrain-api` build contains the customer bootstrap reconciler.
  A box rendered with a bootstrap descriptor but running an image without the
  reconciler starts cleanly and serves an empty workspace.
- The Hetzner project has a free server slot. The limit is per project; MC and
  the broker cannot raise it. Exceeding it returns
  `resource_limit_exceeded / server limit reached` and the run ends in
  `dispatch_failed`.
- `owner_email` is not already a user in Mission Control. See section 7.

## 3. Dry run

The dry-run branch returns before `CustomerProvisioner` executes, so it writes
nothing:

```js
await (await fetch('/api/operator/development-gate/provision', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'same-origin',
  body: JSON.stringify({ owner_email: 'owner@example.com', region: 'nbg1', dry_run: true })
})).json()
```

A returned payload is itself proof of module and image coverage: missing modules
and disallowed images raise before the dry-run return. Confirm
`baseline_source`, `initial_version`, and the `modules` revisions before
continuing.

## 4. Provision

Repeat the call with `dry_run: false`. This bills a server.

Record from the response, and nothing else:

- `provisioning_run.id` — needed for the status poll and the one-time secret read
- `deployment.id` — needed for verification and designation
- `provisioning_run.status` and `failure_reason`

The public host name is the deployment id with underscores replaced by hyphens,
under the fleet base domain. A replacement gate carries a generated suffix, so
it does **not** reuse the previous host name.

Do not copy a non-dry-run response anywhere. It contains a `credentials` array
holding **plaintext service keys**. Treat an accidental copy as a credential
leak and revoke those keys.

## 5. Verify

Poll the run, printing only non-secret fields:

```js
const r = await (await fetch('/api/provisioning/runs/<RUN_ID>', { credentials: 'same-origin' })).json();
({ status: r.status, failure_reason: r.failure_reason, password_ready: r.bootstrap_secret_id ? 'yes' : 'no' })
```

A healthy box reaches `succeeded` within roughly fifteen minutes. When it does,
read the owner password immediately (see the clock in section 6):

```js
await (await fetch('/api/provisioning/runs/<RUN_ID>/bootstrap-secret/read', {
  method: 'POST', credentials: 'same-origin'
})).json()
```

Then sign in at the box's host name and confirm the workspace is populated: one
account, its canonical spaces, its canonical app installations, and a
`customer.bootstrap_reconciled` audit event. An empty Apps page means the
reconciler did not run; do not designate that box.

## 6. The two one-hour clocks

Both default to 3600 seconds and both are silent when they expire.

- **First-boot bootstrap token** (`fleet_bootstrap_token_ttl_seconds`) is
  single-use and expires one hour after dispatch. It is minted only inside
  `HetznerProvisioner.dispatch`; there is **no operator endpoint to re-issue
  one**. The alternative credential, the deployment's fleet key, is delivered
  inside the bundle the box never received. A box that has not completed its
  exchange within the hour is permanently locked out and must be destroyed and
  reprovisioned.
- **Owner one-time password** (`bootstrap_secret_ttl_seconds`) is readable once,
  for one hour after the box reports success. Losing it means losing the only
  login to that box.

Because of the first clock, treat any run still at `dispatched` after fifteen
minutes as an incident, not as slow progress.

## 7. Known traps

- **`duplicate_email`.** `provision()` mints a distinct owner admin user, and
  Mission Control's `users.email` is unique. The super-admin address cannot be
  reused, and every attempt that reaches user creation consumes its address
  permanently. Use a fresh address per attempt.
- **`POST /api/provisioning/runs/{id}/retry` does not work for a Hetzner box.**
  It re-dispatches without `owner_otp`, `owner_email`, or the integration
  credentials, so `validate_bundle` rejects the bundle and the retry fails the
  same way. Recover by provisioning again, not by retrying.
- **Provisioning writes are not transactional.** A failure part-way leaves an
  account, and possibly spaces, apps, a deployment, and service keys, with no
  server behind them. Mission Control will show those modules as `active`.
  Clean them up; do not read the fleet view as evidence a box exists.
- **A `dispatched` run proves only that Hetzner accepted the create call.** It
  says nothing about whether the box ever started.

## 8. Reaching a box

Broker-provisioned boxes have **no SSH**: `hetzner_firewall_allow_ssh` defaults
to false and the default-deny firewall emits no inbound rule for port 22.
Mission Control is reachable by SSH only because it is built by hand.

Use the Hetzner Cloud console: on the server page, `Aktionen` -> reset the root
password, then open the `>_` web terminal and sign in as `root`. That terminal
cannot paste, so keep commands short.

If you need a real terminal, add an inbound TCP 22 rule scoped to your own
address with a `/32` prefix, work over SSH, and **remove the rule afterwards**.
Never open port 22 to `0.0.0.0/0`.

## 9. Diagnosing a box that serves 502

A 502 means Caddy is running and the application behind it is not. Work down
this chain; each step names the next.

1. `docker ps -a` — identify which container is not `Up`. Containers stuck in
   `Created` are waiting on a dependency, not broken themselves. The first
   container that is `Restarting` is the real fault.
2. `docker logs <container> --tail 40` — read that container's error.
3. If Postgres reports `Database is uninitialized and superuser password is not
   specified`, its secrets never arrived. Continue to step 4.
4. `ls -la /opt/onebrain/.env` — this file is written only by a successful
   bundle exchange. Its absence means the box never received any secret, so
   every `${VAR}` reference in `box.env` expanded to empty.
5. `cat /mnt/onebrain-data/onebrain-maintenance/onebrain_update/bootstrap.log` —
   the exchange log. `bootstrap exchange unreachable/rejected; holding` means
   Mission Control refused the credential or was unreachable. The script uses
   `curl -sf`, so the HTTP status is not recorded; re-run the request manually
   if the cause is not obvious from timing.
6. **If that log does not exist at all**, the bootstrap script died before it
   could create it — it loads `box.env` first. Check
   `tail -40 /var/log/cloud-init-output.log` for a shell error naming
   `/opt/onebrain/box.env`.

## 10. box.env must be valid shell

`box.env` is deliberately `.`-sourced by `deploy/box/onebrain_bootstrap.sh` and
`deploy/box/update.sh` so its `${VAR}` references expand from the exchanged
bundle. The literal dotenv loader in `deploy/box/onebrain_dotenv.sh` is used for
`.env` only, precisely because it does not expand.

Every value the renderer writes into `box.env` must therefore be shell-safe.
`_shell_kv` in `app/provisioning/hetzner/render.py` enforces this: whitespace
values are quoted, and a value that could escape its quotes is a render-time
error.

This is not theoretical. On 2026-07-20 a development gate replacement rendered
`UPDATE_PROFILES=onebrain assistant communication` unquoted. The shell parsed
`assistant` as a command, `set -e` killed the bootstrap before it fetched any
secret, Postgres crash-looped with an empty password, and the box served 502
indefinitely while Mission Control still reported the run as `dispatched` and
all eight modules as `active`. Core-only boxes have a single profile and so
never exposed the defect.

## 11. After a failed attempt

- Destroy the server. A box past its bootstrap-token window cannot be recovered.
- Revoke any service keys minted for the dead account. The tenant-scoped
  `DELETE /api/service-keys/{key_id}` route returns 404 for an account the
  caller is not pinned to; revoke by tenant on the Mission Control host instead.
- Remove the orphaned account, deployment, and module rows so the fleet view
  stops describing a box that does not exist.
- Remove any temporary port 22 firewall rule.

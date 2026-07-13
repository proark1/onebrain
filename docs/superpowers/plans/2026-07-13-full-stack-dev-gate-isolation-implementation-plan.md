# Full-stack Development Gate Isolation: Implementation Plan

**Date:** 2026-07-13  
**Status:** Approved design; implementation in progress  
**Design:** `docs/superpowers/specs/2026-07-13-full-stack-dev-gate-isolation-design.md`

## Objective

Replace the current OneBrain-only development release gate with a complete
customer-shaped stack while making fleet/control-plane credentials and routes
unavailable to every customer-facing container and browser user.

The replacement remains the sole automatic release-verification target. A
root-only host agent retains the scope-pinned fleet credential, applies signed
desired state, verifies local module health, and posts a metadata-only
heartbeat. Mission Control remains the only operator/fleet surface.

## Safety invariants

- The development gate uses the `full_stack` bundle: OneBrain API, admin UI,
  workers, Assistant, and Communications API/widget/voice/workers.
- Customer containers contain no fleet key, fleet URL, desired-state signing
  material, callback credential, or Mission Control administrator credential.
- Customer rendering explicitly sets `ONEBRAIN_OPERATOR_MODE=false`,
  `ONEBRAIN_OPERATOR_CONSOLE=false`, and disables the in-process fleet reporter.
- Caddy rejects control-plane paths before its generic `/api/*` handler.
- The host update/heartbeat agent is root-owned, is not a Compose service, and
  is the sole reader of the fleet key from `/opt/onebrain/.env`.
- A replacement is created with a unique temporary deployment ID and hostname;
  it is designated only after the full baseline and smoke tests succeed.
- Every initial module image is an immutable digest included in one trusted,
  production-signed release manifest. No image tag fallback is permitted.
- Candidate registration, customer reconciliation, and customer rollout remain
  disabled during this work.

## Prerequisite for live activation

The current approved release contains only the three OneBrain images. Live
replacement is therefore blocked until Mission Control has an approved,
production-signed manifest that contains exact registry-allowlisted digests and
versions for all eight `full_stack` modules. The code must expose this as a
clear preflight failure; it must never fill missing modules from tags or a
different release.

## Implementation order

### 1. Lock the customer render boundary with tests

**Files**

- Modify `tests/test_hetzner_render.py`.
- Modify `tests/test_api_root.py` as needed for runtime mounting.

**Test first**

- A customer `onebrain-api.env` includes its deployment ID but excludes
  `ONEBRAIN_FLEET_URL`, `ONEBRAIN_FLEET_KEY`, desired-state keys, and all
  operator flags set true.
- Customer rendering explicitly disables both operator surfaces and the
  in-process reporter.
- `box.env` retains fleet URL/key only for the root host update agent.
- A full-stack Caddyfile places the denial handler before generic API routing
  and returns a non-proxy response for `/api/fleet/*`, `/api/operator/*`,
  `/api/provisioning/*`, and `/api/rollouts/*`.

**Implement**

- Make `_module_env` role-aware: only the `operator` role receives the operator
  surface and fleet service variables; customer API containers receive the
  explicit off values above.
- Retain fleet values in the host-only bootstrap/update configuration so the
  existing signed desired-state verifier continues to work without a container
  copy of the key.
- Add the specific Caddy denial handles before `/api/*`; preserve customer APIs,
  admin UI APIs, health, Assistant, and Communications routes.

### 2. Disable application-side fleet reporting for customer stacks

**Files**

- Modify `app/config.py`.
- Modify `app/main.py`.
- Modify `app/fleet/reporter.py` only if its start guard needs a focused helper.
- Modify `tests/test_fleet.py` and `tests/test_api_root.py`.

**Test first**

- `ONEBRAIN_FLEET_REPORTER_ENABLED=false` prevents reporter startup even if a
  development-only test supplies a fleet URL/key.
- Existing operator/self-report behavior remains unchanged when enabled.

**Implement**

- Add `fleet_reporter_enabled` with a backwards-compatible default of `true`.
- Gate `start_reporter` at the application boundary and in the reporter itself
  as defense in depth.
- Customer rendered environments set it false. This makes the absence of the
  credential intentional and testable rather than incidental.

### 3. Add a root-only metadata heartbeat companion

**Files**

- Create `deploy/box/onebrain-gate-report.py`.
- Create `deploy/box/onebrain-gate-agent.sh`.
- Modify `deploy/box/onebrain-update.service` and add/update the paired timer
  only when required for reliable regular reporting.
- Modify `app/provisioning/hetzner/render.py` to install the new host files.
- Modify `tests/test_box_update_sh.py` and `tests/test_hetzner_render.py`.

**Test first**

- Given fixture files and mocked local commands, the report script emits only
  the strict `fleet.v2` metadata contract: deployment ID, build/migration,
  service/module health, update outcome, and uptime. It emits no tenant
  content, account IDs, access tokens, or free-text logs.
- It reports the exact module versions required by
  `verify_development_candidate`, including the full stack.
- Failed local probes or an unreadable migration state mark the heartbeat
  unhealthy; a failed POST cannot disrupt the serving stack.
- The rendered Compose/env files do not contain the fleet key while the root
  configuration has mode `0600` and the agent service runs as root.

**Implement**

- Reuse `update.sh` and `onebrain_box_verify.py` for signed desired-state fetch,
  apply, backup, recovery, and update-state recording; do not create a second
  update engine.
- Have `onebrain-gate-agent.sh` call the existing guarded update script and
  then the reporter. The reporter reads only local Docker/health/migration
  state plus `/data/onebrain_update/update_state.json`, builds JSON with the
  standard library, and posts it directly to Mission Control using the
  root-only fleet key.
- Ensure a regular timer posts a heartbeat even when no desired state is
  offered. Keep logs local and report stable failure state rather than their
  contents.

### 4. Generalize safe full-stack development-gate provisioning

**Files**

- Modify `app/routers/operator.py`.
- Modify `app/provisioning/hetzner/provisioner.py` only for gate-specific
  release-key or validation wiring.
- Modify `app/provisioning/hetzner/render.py`.
- Modify `tests/test_controlplane.py`, `tests/test_provisioning.py`, and
  `tests/test_hetzner_provisioner.py`.

**Test first**

- The development-gate route requires coverage and valid immutable images for
  all `BUNDLES["full_stack"].modules`, and names every missing module in a 409.
- It cannot use a release missing a production signature or an allowlisted
  digest image.
- Replacement creation is permitted while the old designated gate exists, but
  uses a generated unique deployment/account/hostname suffix and is not
  designated initially.
- Existing gate provisioning/dry-run behavior remains idempotent; no arbitrary
  user-supplied deployment ID or bundle becomes possible.
- Designation remains atomic and refuses an unhealthy/incomplete replacement.

**Implement**

- Refactor the private gate-provision construction into a fixed full-stack
  helper. Generate the replacement identity server-side from a constrained
  timestamp/nonce and mark it as an internal development deployment.
- Validate the complete baseline before provisioning begins, including module
  versions, image digest registry allowlist, and production signature.
- Keep `cx23`/current sizing unchanged. Provision local databases/Redis and
  the customer suite exactly as for a normal customer; integrations begin in
  test-safe mode with dummy data.

### 5. Prevent premature release activation and prove the endpoint boundary

**Files**

- Modify `tests/test_release_promotion.py`, `tests/test_fleet.py`, and relevant
  route/render tests.

**Test first and verify**

- A full-stack development heartbeat with matching version, migration, rollout
  attempt, and all module versions can verify a candidate.
- An incomplete baseline, missing module report, unhealthy module, or stale
  update state cannot promote the candidate.
- A browser/customer token receives 404 or no route for every control-plane
  path, while ordinary customer suite APIs remain reachable.
- No test turns on candidate registration or customer reconciliation by
  default.

### 6. Verify, ship code, then execute the live cutover runbook

**Code verification**

Run focused tests during each change, then:

```powershell
py -m pytest -q tests/test_hetzner_render.py tests/test_box_update_sh.py tests/test_fleet.py tests/test_api_root.py tests/test_controlplane.py tests/test_provisioning.py tests/test_hetzner_provisioner.py tests/test_release_promotion.py
py -m pytest -q
```

Review the rendered full-stack output for literals/secrets before staging. If
the worktree is clean apart from task files and tests pass, stage only those
files, commit, push the working branch, fast-forward `main`, merge, and push
`main` following `AGENTS.md`.

**Live activation (after code shipment)**

1. Register/approve a complete production-signed full-stack baseline with exact
   digests for all eight modules.
2. Provision the temporary replacement gate; do not designate it.
3. Confirm public customer paths, all full-stack health checks, local module
   versions, host-agent heartbeat, Caddy 404 control-plane denials, no fleet
   environment variable in any Compose container, and resource headroom.
4. Atomically designate the replacement. Confirm the old gate is no longer the
   target for candidate dispatch.
5. Retire the old dummy gate only after designation and after recording its
   provider resource identifiers. Delete its server/DNS/volume/firewall using
   the recorded erasure manifest and verify the new gate remains healthy.
6. Keep candidate registration and customer rollout automation off until a
   separate explicit activation decision.

## Acceptance matrix

| Scenario | Required result |
| --- | --- |
| Customer container inspection | No fleet/control-plane secret or endpoint configuration |
| Customer browser requests to control paths | 404/denied before application proxying |
| Full stack normal paths | Admin UI, OneBrain, Assistant, and Communications remain reachable |
| Host update agent | Applies only verified signed digest manifests and reports metadata only |
| Missing full-stack release artifact | Provisioning fails before creating provider resources |
| Replacement rollout | New gate stays undesignated until healthy full-stack heartbeat |
| Gate candidate verification | All eight exact module versions and update attempt match |
| Existing customer delivery | Remains disabled and unchanged |
| Resource capacity | Current server size has measured CPU/RAM headroom or cutover stops |

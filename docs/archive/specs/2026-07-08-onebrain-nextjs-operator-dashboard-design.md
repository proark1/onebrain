# OneBrain Next.js Operator Dashboard Design

## Summary

Add a `/operator` route to the Next.js console that reaches parity with the current FastAPI static operator dashboard for provisioning, customer readiness, release planning, service-key management, and rollout operations.

This is Option B: migrate the existing static operator workflows into Next.js using the current Python/FastAPI operator and provisioning endpoints. The backend remains the authority for provisioning, control-plane validation, release manifests, rollout rules, service-key revocation, backup/health records, and admin authorization.

## Goals

- Bring the current operator dashboard into the Next.js console.
- Reuse existing backend endpoints first:
  - `GET /api/provisioning/bundles`
  - `POST /api/provisioning/customers`
  - `GET /api/operator/customers`
  - `GET /api/operator/deployments`
  - `GET /api/operator/deployments/{deployment_id}/modules`
  - `GET /api/operator/releases`
  - `POST /api/operator/releases`
  - `GET /api/operator/deployments/{deployment_id}/update-plan/{target_version}`
  - `GET /api/operator/deployments/{deployment_id}/rollouts`
  - `POST /api/operator/deployments/{deployment_id}/rollouts`
  - `PATCH /api/operator/rollouts/{rollout_id}`
  - `GET /api/operator/deployments/{deployment_id}/backups/latest`
  - `POST /api/operator/deployments/{deployment_id}/backups`
  - `GET /api/operator/deployments/{deployment_id}/health/latest`
  - `POST /api/operator/deployments/{deployment_id}/health`
  - `GET /api/operator/accounts/{account_id}/service-keys`
  - `DELETE /api/operator/accounts/{account_id}/service-keys/{key_id}`
- Keep `/spaces` focused on platform account, space, app-installation, access-check, and audit management.
- Keep `/privacy` focused on export and erase.
- Keep the old static FastAPI frontend available until Next.js covers the necessary workflows and deployment wiring is ready.

## Non-Goals

- Do not add new deployment lifecycle features beyond current static operator parity.
- Do not implement Alembic migrations in this slice.
- Do not implement background workers in this slice.
- Do not change provisioning bundle definitions unless an existing contract bug blocks the UI.
- Do not move provisioning or rollout rules into TypeScript.
- Do not remove the old static frontend yet.

## Existing Backend Contract

`app/routers/operator.py` already provides the control-plane endpoints for:

- customer readiness overview
- deployment listing
- module listing
- release listing and creation
- update-plan checks
- backup and health records
- rollout creation and status updates
- account service-key listing and revoke

`app/routers/provisioning.py` already provides:

- provisioning bundle listing
- customer provisioning
- credential return for newly minted integration keys

The current static UI in `app/static/js/operator.js` uses those endpoints. The first Next.js `/operator` implementation should mirror those workflows rather than expanding the backend surface.

## Needed Additions

Expected Next.js files:

```text
onebrain-web/src/app/
  operator/
    page.tsx

onebrain-web/src/components/
  operator-panel.tsx

onebrain-web/src/lib/
  onebrain-client.ts
  onebrain-types.ts
```

Update shared files:

- `onebrain-web/src/components/console-shell.tsx` to promote `Operator` from future nav to active primary nav.
- `onebrain-web/src/app/globals.css` for operator-specific dense admin layouts.
- `onebrain-web/README.md` to document the route and backend ownership boundary.

No backend endpoint is planned unless implementation uncovers a direct mismatch between the static UI contract and the typed Next.js client.

## Route Behavior

`/operator/page.tsx` uses the existing server session helper.

- If the FastAPI API is unavailable, render `ApiUnavailableState`.
- If the user is not signed in, render `SignedOutState`.
- If the user is not an admin, render a compact blocked state inside `ConsoleShell active="operator"`.
- If the user is an admin, render `ConsoleShell active="operator"` with `OperatorPanel`.

Next.js does not preload sensitive operator data server-side. The client panel loads data through `/api/onebrain/...` so FastAPI sees the signed session cookie and performs authorization.

## UI Design

The operator dashboard is an operational control surface. It should be denser than chat/documents, but still consistent with the OneBrain console visual language: restrained panels, compact stats, clear status chips, and no marketing-style sections.

Primary regions:

1. Header
   - title: `Operator`
   - eyebrow: `Control plane`
   - refresh button

2. Customer summary
   - total customers
   - deployed customers
   - healthy customers
   - attention count
   - active service keys

3. Provisioning
   - bundle list
   - provision customer form
   - provisioned credential output with copy controls

4. Customer readiness
   - account name/id
   - readiness state
   - deployment version/ring/type
   - spaces/apps/keys/modules summary
   - latest backup, health, rollout signals

5. Service keys
   - list account-scoped service keys from customer overview rows
   - revoke active service keys

6. Releases
   - release rail/list
   - create release manifest form from a selected deployment/module set
   - migration/security/rollback fields

7. Deployments and rollouts
   - deployment list
   - module versions
   - latest backup and health state
   - select release target
   - plan update
   - start rollout when allowed
   - record backup
   - record health
   - mark active rollout running/success/failed

The page should use full-width operational bands/panels and avoid nested cards. Long ids, release versions, and module names must wrap cleanly on mobile.

## Data Model Additions

Add typed models matching current backend responses:

- `ProvisioningBundle`
- `ProvisionCustomerInput`
- `ProvisioningResult`
- `ProvisionedCredential`
- `OperatorCustomer`
- `OperatorDeployment`
- `OperatorModule`
- `OperatorRelease`
- `OperatorBackup`
- `OperatorHealth`
- `OperatorRollout`
- `OperatorUpdatePlan`
- `ServiceKeyInfo`

Add typed client helpers:

- `listProvisioningBundles()`
- `provisionCustomer(input)`
- `listOperatorCustomers()`
- `listOperatorDeployments()`
- `listOperatorDeploymentModules(deploymentId)`
- `listOperatorReleases()`
- `createOperatorRelease(input)`
- `getOperatorUpdatePlan(deploymentId, targetVersion)`
- `listOperatorRollouts(deploymentId)`
- `startOperatorRollout(deploymentId, targetVersion)`
- `updateOperatorRollout(rolloutId, input)`
- `latestOperatorBackup(deploymentId)`
- `recordOperatorBackup(deploymentId, input)`
- `latestOperatorHealth(deploymentId)`
- `recordOperatorHealth(deploymentId, input)`
- `listAccountServiceKeys(accountId)`
- `revokeAccountServiceKey(accountId, keyId)`

## Data Flow

Initial load:

1. Load provisioning bundles, customer overview rows, deployments, and releases.
2. For deployment rows, load modules, latest backup, latest health, and recent rollouts in parallel.
3. Derive summary stats from customer rows.
4. Render empty states when no customers, deployments, bundles, or releases exist.

Provision customer:

1. Admin submits customer name, bundle, version, deployment type, region, release ring, and optional account id.
2. FastAPI provisions platform records, deployment records, modules, apps, spaces, audit events, and optionally service keys.
3. The panel refreshes operator data and shows credentials returned by the backend.
4. Credentials are shown only from the immediate provisioning response because service-key secrets are not retrievable later.

Create release:

1. Admin selects a source deployment.
2. The UI previews modules from that deployment.
3. Admin submits version, git SHA, status, migration fields, security notes, rollback plan, and target module version.
4. FastAPI validates module ids and creates the release manifest.
5. The panel refreshes releases and deployments.

Plan and rollout:

1. Admin selects a deployment row and target release.
2. The UI calls the backend update-plan endpoint.
3. If the backend returns `allowed` and modules need updates, show `Start rollout`.
4. Starting a rollout calls FastAPI. Status changes call `PATCH /api/operator/rollouts/{rollout_id}`.
5. Marking success lets FastAPI apply module versions/current deployment version according to backend rules.

Backup and health:

1. Admin records a backup or health check for a deployment.
2. FastAPI validates run status and stores the control-plane record.
3. The panel refreshes deployment readiness.

Service-key revoke:

1. Admin chooses an active key from a customer/account row.
2. The UI calls the operator revoke endpoint.
3. The panel refreshes customer/account state.

## Error Handling

- Session/API unavailable: page-level states.
- Non-admin: blocked page state and backend `403` if a stale client action fires.
- Load failure: inline error and preserve already loaded data where possible.
- Provisioning validation failure: show FastAPI `detail`.
- Duplicate release/version/deployment ids: show FastAPI `detail`.
- Update-plan blocked: render blocked reason from backend, do not enable rollout start.
- Rollout status failure: show backend reason and keep the row visible.
- Clipboard copy failure: show inline notice; credentials remain visible from the response.

## Security And Privacy

- The operator route handles deployment metadata and integration credentials, not customer content.
- Next.js must not log or persist minted service-key secrets.
- Newly minted credentials are shown only in the in-memory UI response.
- Service-key revocation must go through FastAPI.
- Rollout completion must go through FastAPI because it updates module and deployment state.
- All admin checks remain in Python.

## Testing Plan

Automated checks:

- Next.js typecheck
- Next.js lint
- Python pytest suite
- npm audit
- Next.js production build

Runtime smoke:

- `/operator` loads for admin.
- `/operator` blocks non-admin.
- Admin can load bundles, customer overview, deployments, and releases through the Next proxy.
- Non-admin receives `403` from an operator/provisioning endpoint through the Next proxy.
- With temporary ids, admin can provision a customer and receive credentials.
- With temporary release/version data, admin can create a release, record backup, record health, plan an update, start a rollout, and mark it success.
- Admin can revoke an active service key created by the temporary provisioning smoke.

Mutation smoke should use temporary account/deployment/release ids so it does not alter existing production-like records.

## Acceptance Criteria

- `/operator` is visible in the primary console nav.
- `Operator` is no longer shown as a disabled future nav item.
- Admins can perform the existing static operator workflows from Next.js:
  - provision customer
  - inspect customer readiness
  - inspect and revoke service keys
  - create release manifest
  - plan updates
  - record backup and health
  - start rollout
  - update rollout status
- Non-admins see a clear blocked state.
- Backend remains Python/FastAPI authority for all operator behavior.
- Existing `/chat`, `/documents`, `/spaces`, `/privacy`, and workspace selector behavior continue to pass checks.

## Future Work

After this route is shipped, the remaining major product-roadmap slices are:

- Alembic migrations for real schema evolution.
- Background workers for ingestion/retrieval jobs.
- Better retrieval, memory, and task features for the assistant.
- Deployment wiring for the Next.js app alongside FastAPI.
- Retiring the old static FastAPI frontend once Next.js coverage and deployment wiring are stable.


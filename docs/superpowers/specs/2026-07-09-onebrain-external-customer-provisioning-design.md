# OneBrain External Customer Provisioning Design

Date: 2026-07-09

## Summary

Add real customer provisioning from the OneBrain operator console using GitHub
Actions as the external infrastructure runner.

Today the admin can create customer accounts, spaces, app installations,
deployment metadata, module records, audit events, and scoped service keys. The
missing layer is real infrastructure creation: a fresh Railway project, fresh
Postgres database, selected services, environment variables, deployment,
migrations, and smoke checks.

The selected V1 approach keeps OneBrain as the control plane and GitHub Actions
as the infrastructure executor. OneBrain records the provisioning request and
dispatches a workflow. GitHub Actions holds Railway/provider secrets, creates
the Railway stack, deploys it, runs checks, and calls back to OneBrain with
sanitized status and URLs.

## Goals

- Let an admin provision a new customer from the Operator page.
- Create a fresh Railway project per dedicated customer deployment.
- Create fresh Postgres and fresh services for the selected bundle.
- Support OneBrain + Assad Dar AI Communication for one customer shape.
- Support OneBrain + Assad Dar AI Communication + Personal Assistant for the
  fuller customer shape.
- Keep Railway tokens and provider secrets outside the OneBrain backend.
- Track provisioning status, external run links, Railway ids, service URLs,
  migration state, and smoke-check results in the control plane.
- Make failures visible and retryable without pretending a deployment is live.
- Bootstrap the first customer admin for the fresh stack without storing the
  password in deployment metadata.
- Keep the implementation compatible with the current provisioning bundle and
  operator-dashboard contracts.

## Non-Goals

- Do not put broad Railway infrastructure credentials inside the OneBrain web
  backend.
- Do not store customer content in the control plane.
- Do not automatically delete partially created Railway resources in V1.
- Do not build full Terraform/Pulumi infrastructure-as-code in this slice.
- Do not implement module marketplace, billing, customer-owned infrastructure,
  or shared multi-tenant SaaS provisioning in V1.
- Do not redesign the existing account/space/app/service-key provisioning
  model unless needed to attach the external run.

## Selected Approach

Use GitHub Actions workflow dispatch as the external provisioner.

Pros:

- Railway tokens and provider secrets stay in GitHub secrets.
- GitHub gives clear logs, retry controls, audit trails, and permissions.
- The web backend only needs permission to dispatch a known workflow and accept
  authenticated callbacks.
- The first implementation can be tested with dry-run workflow inputs before
  touching Railway.

Trade-offs:

- Provisioning progress is eventually consistent rather than synchronous.
- The operator needs a status view instead of immediate success.
- The workflow becomes part of the release surface and must be validated.

This is the selected approach because it gives the admin the desired one-click
experience while keeping infrastructure authority outside the application.

## Alternatives Considered

### OneBrain Backend Calls Railway Directly

Pros:

- Fewer moving pieces.
- Easier to return immediate responses from the API.

Cons:

- Requires storing powerful Railway credentials in the web backend.
- Makes retries, partial failures, and long-running operations harder.
- Increases blast radius if the app is compromised.

Rejected for V1.

### Dedicated Provisioning Worker Service

Pros:

- Strong long-term option for queues, retries, and custom orchestration.
- Could support more providers later.

Cons:

- Adds another privileged service before the workflow contract is proven.
- Still needs a secret-management and deployment story.

Deferred until the GitHub Actions path proves the product flow.

### Terraform Or Pulumi Pipeline

Pros:

- Strong declarative infrastructure model.
- Better drift detection and review for mature multi-customer operations.

Cons:

- Heavier than the first useful automation.
- Requires modeling the full Railway stack before the control-plane contract is
  stable.

Deferred for a later hardening phase.

## Bundle Mapping

The current bundle ids remain the backend contract for V1.

- `onebrain_only`: fresh OneBrain API, admin UI, workers, and Postgres.
- `onebrain_assistant`: fresh OneBrain stack plus Personal Assistant service.
- `onebrain_communication`: fresh OneBrain stack plus Assad Dar AI
  Communication services.
- `full_stack`: fresh OneBrain stack plus Assad Dar AI Communication and
  Personal Assistant services.

The operator UI may display friendlier labels, but the dispatch payload should
use these stable bundle ids.

## Architecture

The provisioning flow has three actors:

1. Admin operator using the OneBrain Operator page.
2. OneBrain control plane storing metadata and dispatching the workflow.
3. GitHub Actions workflow creating and deploying Railway infrastructure.

Flow:

```text
Admin -> OneBrain Operator -> create customer request
OneBrain -> platform records + provisioning run pending
OneBrain -> GitHub Actions workflow_dispatch
GitHub Actions -> Railway project + Postgres + services + env vars + deploy
GitHub Actions -> migrations and smoke checks
GitHub Actions -> OneBrain callback with sanitized result
Operator page -> provisioning status, URLs, failures, retry affordance
```

OneBrain remains authoritative for:

- customer account metadata
- spaces and app installations
- scoped service-key records
- deployment/module registry
- provisioning-run status
- operator visibility

GitHub Actions remains authoritative for:

- Railway token usage
- Railway project/service/database creation
- service environment variable injection
- deployment commands
- workflow logs
- smoke-test execution

## Data Model

Add provisioning-run metadata to the control-plane domain. A provisioning run is
separate from `CustomerDeployment` because an account can exist while
infrastructure is still pending, running, or failed.

Suggested fields:

- `id`
- `account_id`
- `deployment_id`
- `bundle_id`
- `customer_name`
- `status`: `pending`, `dispatched`, `running`, `succeeded`, `failed`,
  `cancelled`
- `external_provider`: `github_actions`
- `external_run_id`
- `external_run_url`
- `railway_project_id`
- `railway_environment_id`
- `railway_project_url`
- `api_url`
- `admin_ui_url`
- `assistant_url`
- `communication_url`
- `current_migration`
- `health_status`
- `smoke_status`
- `failure_message`
- `bootstrap_secret_id`
- `created_by`
- `created_at`
- `updated_at`

The run may also store a sanitized `service_ids` map keyed by module id:

```json
{
  "onebrain-api": "railway-service-id",
  "onebrain-admin-ui": "railway-service-id",
  "onebrain-workers": "railway-service-id",
  "communication-api": "railway-service-id",
  "assistant-service": "railway-service-id"
}
```

No Railway token, provider secret, generated password, service-key plaintext,
or customer content is stored in this deployment metadata. A generated bootstrap
password, when used, is held only in a separate one-time secret envelope with a
short TTL and first-read deletion.

## API Contract

The existing endpoint remains the admin entry point:

```text
POST /api/provisioning/customers
```

The request gains one required infrastructure field when external provisioning
is enabled:

- `customer_admin_email`

V1 behavior:

1. Validate admin principal.
2. Create account, spaces, app installations, deployment, modules, audit event,
   and scoped integration keys using the existing provisioning service.
3. Create a provisioning run with `pending` status.
4. Dispatch the GitHub Actions workflow.
5. Mark the run `dispatched` when GitHub accepts the workflow request.
6. Return the normal provisioning response plus `provisioning_run`.

New endpoints:

```text
GET /api/provisioning/runs
GET /api/provisioning/runs/{run_id}
POST /api/provisioning/runs/{run_id}/retry
POST /api/provisioning/runs/{run_id}/callback
```

`GET` endpoints require an admin human principal.

Retry is admin-only and allowed only from `failed` or `cancelled` runs. It does
not recreate accounts, spaces, app installations, service keys, deployments, or
modules. It creates a new provisioning run for the same account/deployment and
dispatches the workflow again with the already recorded bundle and deployment
metadata.

The callback endpoint uses service authentication, not a human session. The
callback key is generated for infrastructure automation and stored only as a
hash in OneBrain. GitHub Actions sends:

```json
{
  "status": "running",
  "external_run_id": "123456789",
  "external_run_url": "https://github.com/org/repo/actions/runs/123456789",
  "railway_project_id": "project-id",
  "railway_environment_id": "environment-id",
  "railway_project_url": "https://railway.app/project/project-id",
  "service_ids": {
    "onebrain-api": "service-id"
  },
  "urls": {
    "api": "https://...",
    "admin_ui": "https://..."
  },
  "current_migration": "0002_postgres_worker_jobs",
  "health_status": "success",
  "smoke_status": "success",
  "bootstrap_password": "<one-time plaintext, accepted only on success>",
  "failure_message": ""
}
```

Callback updates are idempotent by `run_id` and status progression. Terminal
states cannot be overwritten by older non-terminal callbacks.

`bootstrap_password` is accepted only with a `succeeded` callback. The API
stores it in the encrypted one-time secret envelope and redacts it from logs,
audit metadata, status responses, and control-plane records.

## GitHub Actions Workflow

Add:

```text
.github/workflows/provision-customer.yml
```

Inputs:

- `provisioning_run_id`
- `account_id`
- `deployment_id`
- `customer_name`
- `bundle_id`
- `region`
- `initial_version`
- `customer_admin_email`
- `callback_url`
- `git_ref`
- `dry_run`

Secrets:

- `RAILWAY_TOKEN`
- `ONEBRAIN_PROVISIONING_CALLBACK_KEY`
- provider API keys needed by the new stack
- secret material for encrypting one-time bootstrap credentials

Workflow steps:

1. Validate inputs and bundle id.
2. Install Railway CLI.
3. Send `running` callback.
4. Create a Railway project named from customer/deployment id.
5. Add a Postgres database.
6. Create services for the selected bundle.
7. Set service environment variables.
8. Connect services to this repository/ref.
9. Deploy the services.
10. Let API startup run Alembic migrations.
11. Smoke-check API `/health`, admin UI health, and enabled module endpoints.
12. Send `succeeded` callback with Railway ids, URLs, migration, and smoke
    results.
13. On failure, send `failed` callback with sanitized failure detail and any
    created Railway ids.

The workflow must mask tokens, generated passwords, provider keys, and callback
keys in logs.

## Railway Stack Shape

Every dedicated customer stack gets a new Railway project and one Postgres
database.

Core services:

- `onebrain-api`
- `onebrain-admin-ui`
- `onebrain-workers`
- `Postgres`

Assistant services when included:

- `assistant-service`

Communication services when included:

- `communication-api`
- `communication-widget`
- `communication-voice`
- `communication-workers`

Shared service variables should match `docs/deployment.md`, including:

- `ONEBRAIN_VECTOR_STORE=pgvector`
- `ONEBRAIN_DATABASE_URL=${{Postgres.DATABASE_URL}}`
- `ONEBRAIN_AUTH_SECRET`
- `ONEBRAIN_COOKIE_SECURE=true`
- `ONEBRAIN_LLM_PROVIDER`
- `ONEBRAIN_EMBEDDINGS_PROVIDER`
- provider API keys
- `ONEBRAIN_ADMIN_EMAIL`
- `ONEBRAIN_ADMIN_PASSWORD`
- privacy and approval gates

Next.js receives:

- `ONEBRAIN_API_BASE_URL=http://${{onebrain-api.RAILWAY_PRIVATE_DOMAIN}}:8080`

The API receives:

- `ONEBRAIN_ADMIN_UI_URL=https://<admin-ui-domain>`
- `ONEBRAIN_LEGACY_STATIC_UI_ENABLED=false`

## Admin Bootstrap

V1 requires `customer_admin_email` in the operator form. The GitHub Actions
workflow generates a strong one-time password, sets it as
`ONEBRAIN_ADMIN_PASSWORD` on the new Railway API and worker services, and
returns it to OneBrain through the authenticated success callback.

OneBrain stores that password only in an encrypted one-time secret envelope
linked from `bootstrap_secret_id`. The secret has a short TTL, is shown only to
an admin, and is deleted on first read. The password is never stored in the
control-plane deployment metadata, audit metadata, workflow logs, or service-key
records.

If the one-time secret envelope cannot be created, the provisioning run remains
`failed` even if Railway resources were created. That failure is safer than
creating a customer stack nobody can log into.

## Error Handling

- If validation fails before dispatch, no provisioning run is dispatched.
- If local account/platform provisioning succeeds but workflow dispatch fails,
  the run is marked `failed` with `dispatch_failed`.
- If Railway creation partially succeeds, the workflow reports any created
  project/service ids in the failure callback.
- V1 does not auto-delete partially created Railway resources.
- Retry creates a new provisioning run for the same deployment after operator
  confirmation and does not recreate local platform records.
- Terminal `succeeded`, `failed`, and `cancelled` statuses are protected from
  stale callback overwrites.
- The operator page shows failure messages without exposing raw logs or
  secrets.

## Security And Privacy

- Only human admins can start provisioning.
- The callback endpoint is authenticated with a dedicated provisioning callback
  credential.
- The callback credential is hashed at rest and may be rotated.
- OneBrain does not store Railway tokens, provider secrets, or customer content.
- Generated bootstrap passwords are stored only in encrypted one-time secret
  envelopes with short TTL and first-read deletion.
- Workflow logs must mask secrets and avoid printing full environment dumps.
- The control plane stores deployment metadata only.
- Provisioning audit events record who started the run and the selected bundle.
- Service-key plaintext remains visible only in the immediate provisioning
  response, matching the existing service-key behavior.

## Testing Plan

Automated backend tests:

- Provisioning a customer creates a provisioning run.
- Non-admin users cannot create or list provisioning runs.
- GitHub dispatch failures mark the run failed without claiming deployment
  success.
- Callback authentication is required.
- Callback updates status, Railway ids, URLs, migration, and smoke state.
- Terminal status cannot be overwritten by stale callbacks.
- Failed or cancelled provisioning runs can be retried without duplicating local
  customer records.
- One-time bootstrap passwords are encrypted, expire, and are deleted on first
  read.
- Existing bundle tests still pass.

Workflow checks:

- Validate workflow YAML syntax.
- Run dry-run mode with sample bundle inputs.
- Verify dry-run emits callbacks without calling Railway.
- Verify bundle-to-service mapping for `onebrain_communication` and
  `full_stack`.

Manual smoke for first real Railway stack:

1. Provision a temporary synthetic-data customer.
2. Confirm GitHub Actions run is linked in the operator view.
3. Confirm Railway project, Postgres, API, admin UI, workers, and enabled
   module services are created.
4. Confirm API `/health` succeeds.
5. Confirm admin UI health succeeds.
6. Confirm migration revision is recorded.
7. Confirm the operator page shows `succeeded` with service URLs.

## Acceptance Criteria

- Admin can start a real external provisioning run from OneBrain.
- GitHub Actions receives the provisioning request and owns Railway execution.
- A new Railway project and fresh Postgres are created for a dedicated customer.
- Selected bundle services are created and deployed.
- OneBrain records provisioning status and external run metadata.
- Successful provisioning records Railway ids, service URLs, migration, and
  smoke status.
- Failed provisioning records a sanitized reason and any partial Railway ids.
- Failed provisioning can be retried without recreating local account records.
- The first customer admin credential is delivered as a one-time secret and is
  not stored in deployment metadata.
- Railway/provider secrets remain outside OneBrain.
- Tests cover backend status transitions, admin authorization, callback auth,
  and dry-run workflow behavior.

## Follow-Up Work

- Add explicit cleanup/delete action for failed partial Railway stacks.
- Add Terraform or Pulumi once the workflow contract stabilizes.
- Add richer provisioning progress events instead of a single run status.
- Add customer admin invitation flow instead of bootstrap passwords.
- Add per-customer maintenance windows and rollout policies to the same
  control-plane view.

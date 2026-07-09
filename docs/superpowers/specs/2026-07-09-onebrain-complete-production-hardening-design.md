# OneBrain Complete Production Hardening Design

## Summary

This design completes the remaining production-readiness work identified after
the July 8 and July 9 implementation slices. The current repo already has the
Next.js console, Alembic, Postgres-backed core stores, worker jobs, scoped
service keys, assistant records, brand themes, privacy export/erase, deployment
docs, and a local operator control plane.

The missing layer is production hardening across five areas:

- durable Postgres control-plane state,
- external customer infrastructure provisioning through GitHub Actions and
  Railway,
- first-class governance records and retention enforcement,
- operator UI and observability surfaces for the new state,
- CI gates for frontend, migrations, workflow syntax, and secret scanning.

The implementation should be phased but complete. Each phase must pass tests and
ship independently, while preserving the full destination described here.

## Goals

- Persist all operator control-plane records in Postgres when OneBrain runs in
  Postgres mode.
- Add provisioning-run tracking that can dispatch GitHub Actions, receive
  authenticated callbacks, retry failed runs, and avoid stale terminal-state
  overwrites.
- Add a real `.github/workflows/provision-customer.yml` workflow that supports
  dry-run mode and real Railway provisioning when secrets are present.
- Keep Railway tokens, provider keys, and other broad infrastructure secrets out
  of the OneBrain backend.
- Store bootstrap admin credentials only as encrypted one-time secret envelopes
  with expiry and first-read deletion.
- Add governance records for organizations, memberships, consent, retention
  policies, data-access events, processor/provider registrations, and encrypted
  credential metadata.
- Extend privacy export/delete and retention workers to cover the new governance
  and control-plane data where appropriate.
- Expand the Next.js operator and privacy/admin surfaces so admins can inspect
  the new state without using raw API calls.
- Harden CI with backend tests, frontend checks, Alembic validation, workflow
  validation, and secret scanning.

## Non-Goals

- Do not replace GitHub Actions with Terraform or Pulumi in this pass.
- Do not move Railway/provider tokens into FastAPI or the database.
- Do not implement customer-content inspection in the operator dashboard.
- Do not turn OneBrain into a full external secrets manager. One-time bootstrap
  envelopes are only for short-lived delivery of generated first-login
  credentials.
- Do not require real Railway provisioning in local development or pull-request
  CI. Dry-run mode must exercise the contract without external side effects.

## Current State

The repo is clean and all existing checks pass:

- `python -m pytest -q`
- `npm run lint`
- `npm run typecheck`
- `npm run build`

Already implemented:

- FastAPI API with production-safe startup checks.
- Next.js console for chat, documents, spaces, privacy, operator, and workspace
  selection.
- Alembic migrations through `0004_brand_theme_provisioning`.
- Postgres-backed stores for platform, vector chunks, users, conversations,
  intake, jobs, and service keys.
- Memory/JSON-backed control-plane store.
- Local customer provisioning that creates accounts, spaces, app installations,
  deployments, modules, service keys, and brand themes.
- Scoped service-key lifecycle metadata, revoke, rotate, and safe usage counts.
- Account/space privacy export and erase for documents, conversations, and
  intake records.
- Scoped assistant records over intake records.
- Retrieval score filtering and short-term retrieval history.
- Railway deployment documentation and Dockerfiles for API, worker, and Next.js.

Still missing:

- Postgres-backed control-plane store and migration.
- Provisioning-run records, callback authentication, callback endpoint, retry,
  and dispatch state.
- GitHub Actions workflow for dry-run and real Railway provisioning.
- One-time encrypted bootstrap-secret envelopes.
- Governance tables and APIs beyond accounts/spaces/apps/audit.
- Retention workers for governance and domain data.
- RLS as an enforced migration.
- Operator UI for provisioning runs, key lifecycle details, governance records,
  and richer observability.
- CI coverage for frontend, migration checks, workflow validation, and secret
  scanning.

## Chosen Approach

Use a phased full implementation. The destination includes every missing area,
but each phase lands behind clear contracts and tests:

1. Durable control-plane schema and store.
2. Provisioning-run backend and one-time secret envelopes.
3. GitHub Actions dry-run and real Railway workflow.
4. Governance schema, APIs, privacy coverage, and retention worker hooks.
5. Operator and privacy/admin UI expansion.
6. RLS and CI hardening.

This avoids building external automation on top of volatile local state and
keeps real infrastructure creation optional until secrets are configured.

## Architecture

OneBrain remains split into three runtime roles:

- FastAPI owns product authority, auth, platform records, provisioning-run state,
  callback validation, and operator APIs.
- Next.js owns the browser console and proxies browser requests to FastAPI.
- GitHub Actions owns privileged infrastructure execution against Railway and
  provider APIs.

FastAPI dispatches a workflow with a sanitized provisioning payload and stores a
local provisioning run. GitHub Actions creates or dry-runs the external stack and
calls FastAPI back with sanitized status, external ids, URLs, migration version,
smoke-check results, and a one-time bootstrap password only on success.

The control plane stores metadata only. Customer content remains in the customer
data plane and is not exposed through operator workflows.

## Phase 1: Durable Control Plane

### Data Model

Add migration `0005_control_plane_postgres` with tables:

- `control_deployments`
- `control_deployment_modules`
- `control_release_manifests`
- `control_backups`
- `control_health_checks`
- `control_rollouts`

Fields mirror the existing dataclasses in `app/controlplane/base.py`.

`control_release_manifests.modules` should be `JSONB` because release manifests
map module ids to versions. Backup, health, and rollout detail/notes fields stay
plain text. Tables must have indexes on deployment id, target version, status,
and created timestamps where the operator UI lists recent activity.

### Store

Add `app/controlplane/postgres.py` implementing the existing
`ControlPlaneStore` protocol.

Update `app/controlplane/factory.py`:

- memory mode keeps the current `MemoryControlPlaneStore`,
- Postgres mode returns `PostgresControlPlaneStore`,
- Postgres construction validates Alembic head before use.

The Postgres implementation must preserve existing behavior:

- duplicate deployments and releases fail,
- modules require an existing deployment,
- update plans require a release and installed modules,
- schema updates require a successful latest backup,
- successful rollouts update module versions and deployment migration/version,
- terminal rollout statuses cannot be overwritten.

### Tests

- Reuse current control-plane behavior tests against memory.
- Add fake-connection or integration-style tests for SQL shape where practical.
- Add schema validation tests for new tables.
- Add migration head tests so Postgres mode requires `0005`.

## Phase 2: Provisioning Runs And One-Time Secrets

### Data Model

Add migration `0006_provisioning_runs` with tables:

- `provisioning_runs`
- `one_time_secret_envelopes`

`provisioning_runs` fields:

- `id`
- `account_id`
- `deployment_id`
- `bundle_id`
- `requested_by`
- `status`
- `external_provider`
- `external_run_id`
- `external_run_url`
- `request_payload`
- `result_payload`
- `railway_project_id`
- `railway_environment_id`
- `service_urls`
- `migration_revision`
- `smoke_status`
- `failure_reason`
- `bootstrap_secret_id`
- `retry_of_run_id`
- `created_at`
- `updated_at`
- `dispatched_at`
- `completed_at`

Allowed statuses:

- `pending`
- `dispatch_failed`
- `dispatched`
- `running`
- `succeeded`
- `failed`
- `cancelled`

Terminal statuses:

- `succeeded`
- `failed`
- `cancelled`
- `dispatch_failed`

`one_time_secret_envelopes` fields:

- `id`
- `purpose`
- `account_id`
- `deployment_id`
- `ciphertext`
- `nonce`
- `key_version`
- `expires_at`
- `read_at`
- `created_at`

The plaintext bootstrap password must never be stored in provisioning metadata,
audit metadata, workflow logs, or test fixtures. The envelope is readable once
by an admin before expiry.

### Encryption

Add settings:

- `ONEBRAIN_SECRET_ENCRYPTION_KEY`
- `ONEBRAIN_SECRET_ENCRYPTION_KEY_VERSION`
- `ONEBRAIN_BOOTSTRAP_SECRET_TTL_SECONDS`

The encryption key should be base64url or hex encoded. Startup in production
must fail if one-time secret envelopes are enabled without a strong encryption
key. Tests can use a deterministic test key.

Use `cryptography` with Fernet/MultiFernet for authenticated encryption and key
rotation support. Add `cryptography` to the Python dependencies as part of the
one-time secret phase rather than hand-rolling encryption.

### API

Add admin endpoints:

- `GET /api/provisioning/runs`
- `GET /api/provisioning/runs/{run_id}`
- `POST /api/provisioning/runs/{run_id}/retry`
- `POST /api/provisioning/runs/{run_id}/bootstrap-secret/read`

Add service/callback endpoint:

- `POST /api/provisioning/runs/{run_id}/callback`

Callback authentication uses a dedicated hashed provisioning callback key, not a
human session and not a general service key. Settings:

- `ONEBRAIN_PROVISIONING_CALLBACK_KEY_HASH`
- `ONEBRAIN_PROVISIONING_CALLBACK_KEY_ID`

The callback endpoint accepts:

- status,
- external run id and URL,
- Railway ids,
- service URLs,
- migration revision,
- smoke status,
- sanitized failure reason,
- bootstrap password only with `succeeded`.

Callback rules:

- Unknown runs return `404`.
- Invalid callback auth returns `401`.
- Non-terminal callbacks cannot overwrite terminal runs.
- Older callbacks cannot move a run backward.
- Bootstrap passwords are accepted only with `succeeded`.
- If encrypting the bootstrap secret fails, the run becomes `failed` even if
  external infrastructure succeeded.

### Dispatch

Provisioning a dedicated customer should create local records, create a
`pending` run, dispatch GitHub Actions, and then mark the run `dispatched` or
`dispatch_failed`.

Dispatch uses a narrow GitHub token configured in FastAPI:

- `ONEBRAIN_GITHUB_OWNER`
- `ONEBRAIN_GITHUB_REPO`
- `ONEBRAIN_GITHUB_WORKFLOW`
- `ONEBRAIN_GITHUB_REF`
- `ONEBRAIN_GITHUB_DISPATCH_TOKEN`

The dispatch token can call only the selected workflow. If dispatch settings are
absent, the API supports local provisioning but marks external provisioning as
disabled with a clear admin-facing error.

### Tests

- Creating a provisioning run.
- Admin-only run listing and retry.
- Dispatch success and dispatch failure.
- Callback authentication.
- Callback status transitions.
- Terminal-state overwrite refusal.
- Bootstrap secret encryption, expiry, first-read deletion, and redaction.
- Retry creates a new run without duplicating account/platform records.

## Phase 3: GitHub Actions Railway Workflow

Add `.github/workflows/provision-customer.yml`.

### Inputs

Workflow dispatch inputs:

- `run_id`
- `account_id`
- `deployment_id`
- `customer_name`
- `bundle_id`
- `deployment_type`
- `region`
- `release_ring`
- `initial_version`
- `module_versions_json`
- `brand_theme_json`
- `callback_url`
- `callback_key_id`
- `dry_run`

### Secrets

Workflow secrets:

- `RAILWAY_TOKEN`
- `ONEBRAIN_PROVISIONING_CALLBACK_KEY`
- provider API keys needed by the customer stack,
- optional organization/team ids if Railway requires them.

Secrets must be masked immediately. The workflow must never echo environment
dumps or plaintext bootstrap passwords.

### Dry-Run Mode

Dry-run mode:

- validates inputs,
- derives the services that would be created,
- generates synthetic Railway ids and URLs,
- sends `running` and `succeeded` callbacks,
- does not call Railway.

CI uses dry-run mode or static validation only.

### Real Mode

Real mode:

1. Validate inputs.
2. Install Railway CLI.
3. Send `running` callback.
4. Create Railway project and environment.
5. Add Postgres with pgvector.
6. Create API, worker, and Next.js services.
7. Create enabled module services from the selected bundle.
8. Configure environment variables.
9. Generate a strong bootstrap admin password.
10. Deploy services.
11. Run health, migration, admin UI, and optional service-key smoke checks.
12. Send `succeeded` callback with sanitized metadata and bootstrap password.
13. On failure, send `failed` callback with sanitized reason and any known ids.

The exact Railway CLI commands should be implemented in small shell steps with
strict error handling and no printed secrets.

### Tests And Validation

- Workflow YAML parses.
- Dry-run validates sample payload.
- Dry-run callback payload matches backend schema.
- Bundle-to-service mapping is deterministic.
- Workflow paths are included in CI.

## Phase 4: Governance, Privacy, And Retention

### Data Model

Add migration `0007_governance_privacy_retention` with tables:

- `platform_organizations`
- `platform_memberships`
- `platform_consent_records`
- `platform_retention_policies`
- `platform_data_access_events`
- `platform_processor_register`
- `platform_provider_register`
- `platform_credential_metadata`
- `retention_runs`

These tables complement, not replace, existing accounts, spaces, app
installations, and audit events.

### Records

Organizations:

- model business entities under accounts,
- can be linked to one or more accounts.

Memberships:

- link users to accounts, organizations, spaces, and roles,
- expose status and timestamps,
- do not replace existing simple session roles immediately.

Consent records:

- account id,
- optional space id,
- subject reference,
- purpose,
- status,
- source,
- captured by,
- timestamp,
- withdrawal timestamp.

Retention policies:

- account id,
- optional space id,
- domain,
- record type,
- action,
- duration days,
- legal basis,
- status.

Data access events:

- actor,
- actor type,
- account id,
- space id,
- app id,
- purpose,
- target type,
- target id,
- action,
- decision,
- metadata with no customer content.

Processor/provider registers:

- provider name,
- category,
- region,
- DPA/DPIA status,
- transfer mechanism,
- status,
- metadata.

Credential metadata:

- provider,
- account id,
- app id,
- status,
- secret reference,
- rotation timestamps,
- last verified timestamp,
- no plaintext credential values.

### API

Add admin endpoints under `/api/platform` or `/api/privacy`:

- list/create/update organizations,
- list/create/update memberships,
- list/create/update consent records,
- list/create/update retention policies,
- list data-access events,
- list/create/update processor/provider registrations,
- list/create/update credential metadata,
- start/list retention runs.

Sensitive operations require admin. Service-level data-access event recording can
be allowed for scoped service principals when it only records their own access.

### Retention Worker

Add retention job handling to the worker layer:

- enqueue retention run by account/space/domain,
- find expired records according to active retention policies,
- delete or redact supported domains,
- record counts and audit events,
- never delete outside the requested account/space,
- support dry-run mode that reports counts without deletion.

Initial supported domains:

- documents/chunks,
- conversations/messages,
- intake records,
- governance consent records where policy allows archive or delete,
- data-access events according to audit retention policy.

### Privacy Export And Erase

Extend privacy export to include:

- organizations,
- memberships,
- consent records,
- retention policies,
- data-access events,
- processor/provider registrations,
- credential metadata without secrets,
- relevant control-plane metadata.

Extend erase to delete or redact account/space-scoped governance records where
legally allowed. Processor/provider registers are global operational records and
should not be erased by customer account deletion; exports can include relevant
linked entries.

## Phase 5: Operator And Admin UI

### Operator

Expand `onebrain-web/src/components/operator-panel.tsx` or split it into
focused components if it has become too large.

Add operator views for:

- provisioning runs with status, callback metadata, retry action, and external
  run URL,
- one-time bootstrap secret read action with confirmation and expiry state,
- service-key lifecycle details including last use, use count, revoke, and
  rotate,
- control-plane persistence details for deployments, releases, backups, health,
  rollouts,
- observability summaries for jobs, service keys, storage, retrieval, and
  provisioning runs.

The operator UI must not display customer content.

### Privacy/Admin

Expand privacy/spaces/admin surfaces for:

- consent records,
- retention policies,
- retention run start and dry-run results,
- data-access event lists,
- processor/provider register,
- credential metadata list.

Use existing console shell and Next.js proxy patterns. Avoid adding a second
frontend state architecture.

### UI Tests

The repo currently has no browser test framework. Add focused component-light
coverage only if the existing toolchain supports it without excessive setup.
Otherwise rely on TypeScript, lint, build, and backend contract tests in this
phase.

## Phase 6: RLS And CI Hardening

### RLS

Turn `scripts/enable_rls.sql` into an enforced Alembic hardening migration once
the service roles and store session variables are ready.

Add settings:

- `ONEBRAIN_POSTGRES_APP_ROLE`
- `ONEBRAIN_POSTGRES_SERVICE_ROLE`
- `ONEBRAIN_RLS_ENFORCED`

Store calls in Postgres mode must set tenant/account/space context in a single
database session before reading customer-scoped tables. Startup should fail in
production Postgres mode when RLS is required but not active.

If RLS cannot safely land in the same PR as governance, it should be a separate
phase after all stores are using explicit scope context. It remains part of this
complete hardening program.

### CI

Expand `.github/workflows/tests.yml`:

- Python tests.
- Next.js `npm ci`, `npm run lint`, `npm run typecheck`, `npm run build`.
- Alembic migration validation.
- Workflow YAML validation for provisioning workflow.
- Secret scanning with a lightweight tool or deterministic regex gate.

Add an optional Postgres service job when pgvector can be installed reliably in
CI. If pgvector setup is flaky, keep migration SQL validation in required CI and
add documented manual Postgres smoke until a stable service image is selected.

## Error Handling

- Provisioning dispatch failures create visible `dispatch_failed` runs with a
  sanitized reason.
- Callback failures never expose secrets or raw workflow logs.
- Retention runs report per-domain counts and failed domain summaries without
  raw content.
- Governance writes validate account, space, app, purpose, and status values.
- UI mutation failures keep existing state visible and show the backend error.
- Migration mismatch errors keep the current clear Postgres guidance.

## Security And Privacy

- Service keys remain hash-only at rest.
- Infrastructure secrets stay in GitHub Actions secrets.
- Bootstrap passwords are encrypted, single-read, short-lived, and redacted from
  logs and metadata.
- Operator endpoints remain admin-only unless explicitly callback/service
  authenticated.
- Customer content is not displayed in operator control-plane views.
- Governance metadata avoids raw customer content.
- Privacy export/delete includes source and derived data covered by the current
  stores.
- RLS is added only after scope context is consistently applied to Postgres
  sessions.

## Testing Plan

Backend:

- Existing full test suite remains green.
- Control-plane memory and Postgres behavior.
- New migrations and schema validation.
- Provisioning-run state transitions.
- GitHub dispatch success/failure with mocked HTTP.
- Callback auth and stale callback behavior.
- One-time secret encryption, expiry, and first-read deletion.
- Governance CRUD and authorization.
- Privacy export/delete coverage for governance records.
- Retention worker dry-run and destructive modes.
- RLS startup checks when enabled.

Frontend:

- `npm run lint`
- `npm run typecheck`
- `npm run build`
- Manual local smoke for operator and privacy/admin views.

Workflow:

- YAML syntax validation.
- Dry-run input validation.
- Dry-run callback payload validation against backend schema.
- Manual real Railway smoke with temporary synthetic-data customer.

## Rollout Plan

1. Land control-plane Postgres persistence and tests.
2. Land provisioning-run backend, callback auth, one-time secrets, and tests.
3. Land GitHub Actions dry-run workflow and CI validation.
4. Land real Railway workflow commands behind required secrets and manual smoke.
5. Land governance schema/APIs and privacy export/delete coverage.
6. Land retention worker support.
7. Land operator/admin UI expansion.
8. Land RLS enforcement once all Postgres stores have explicit scope context.
9. Expand CI gates and update deployment docs.

Each step should be shippable and leave production defaults safe. Real customer
data remains blocked by the synthetic-data phase until DPIA, processor register,
retention policies, and provider routing are approved.

## Acceptance Criteria

- All existing backend and frontend checks pass.
- Postgres mode persists control-plane deployments, releases, backups, health
  checks, and rollouts.
- Admins can start, inspect, retry, and receive callbacks for provisioning runs.
- GitHub Actions can dry-run provisioning and send valid callbacks.
- GitHub Actions can create a real Railway customer stack when required secrets
  are configured.
- One-time bootstrap credentials are encrypted, expire, and are deleted on first
  read.
- Governance records exist and are protected by admin/service authorization.
- Privacy export includes supported governance and control-plane metadata.
- Privacy erase/delete or redaction respects account and space scope.
- Retention workers can dry-run and enforce active policies for supported
  domains.
- Operator UI exposes provisioning, lifecycle, observability, and governance
  state without customer content.
- RLS enforcement is available and fails closed when required.
- CI runs backend, frontend, migration, workflow, and secret-scan checks.

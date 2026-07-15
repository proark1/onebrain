# Live KPI Dashboard Design

**Date:** 2026-07-16
**Status:** Approved
**Branch:** `codex/kpi-live-integration`

## Objective

Replace the KPI dashboard's hard-coded sample values with a complete, tenant-isolated KPI capability. OneBrain will own KPI definitions and immutable snapshot history. External systems will submit new values through an authenticated ingestion API; the dashboard will read only data stored in OneBrain.

The first release is vendor-neutral. It does not store ERP, CRM, billing, or HRIS credentials and does not call arbitrary external URLs. Vendor-specific connector jobs can be added later without changing the KPI storage or dashboard contracts.

## Success criteria

The feature is complete when:

- authorized users can discover KPI-enabled workspaces;
- account administrators can create, edit, and archive KPI definitions;
- authorized service keys can submit bounded batches of KPI snapshots;
- administrators can submit a single manual snapshot for setup verification;
- dashboard readers see current values, previous-value deltas, freshness, threshold state, and bounded history from persisted data;
- retries are idempotent and snapshots are immutable;
- account, space, app, purpose, and object authorization is enforced on every operation;
- Postgres row-level security protects both KPI tables;
- privacy export, erasure, retention, and legal holds include KPI data;
- the UI never presents sample values as live data; and
- backend and frontend checks cover the new behavior, including adversarial isolation cases.

## Non-goals

- Direct HubSpot, Xero, Salesforce, Stripe, HRIS, or other vendor connectors.
- A formula engine for KPIs derived from other KPIs.
- Alert delivery by email, chat, or webhook.
- Arbitrary per-snapshot metadata or file attachments.
- Cross-customer Mission Control analytics.
- User-defined executable expressions or remote callback URLs.

## Architecture

### Dedicated KPI domain

KPI data will live in a dedicated `app/kpis` domain rather than expanding the platform store. The domain will contain:

- immutable definition and snapshot models;
- validation and threshold-evaluation functions;
- a narrow store protocol;
- JSON-backed memory persistence for local development and tests;
- a Postgres implementation for deployed environments; and
- a factory wired through `app.deps`.

Keeping KPI persistence separate limits failure coupling with platform governance data and keeps each store contract understandable. The memory backend remains a local/test backend; production deployments continue to use Postgres.

### API surfaces

Human session endpoints will use an `/api/kpis` router. Machine ingestion will use a separate `/api/service/kpis/snapshots` endpoint authenticated exclusively by scoped service keys. The service endpoint is intended to be called directly by connector jobs, not through the browser UI.

The application will mount both routers explicitly. The service principal usage registry will receive a named KPI-ingestion operation so key usage is observable rather than recorded as `service.unknown`.

### Request flow

1. An administrator creates a KPI definition in an authorized account and space.
2. A connector transforms a source-system value into OneBrain's fixed snapshot schema.
3. The connector sends a bounded batch with its app-pinned service key.
4. OneBrain derives the account and app from the key, validates the requested space and every referenced KPI, checks the installed app purpose, applies rate and payload limits, and writes the batch transactionally.
5. The dashboard requests one bounded summary for the selected workspace.
6. The store returns definitions plus latest, previous, and history points without per-tile queries.

## Data model

### KPI definition

Each definition has:

- `id`: server-generated opaque identifier;
- `account_id` and `space_id`: mandatory ownership boundary;
- `key`: stable lowercase key matching `^[a-z][a-z0-9_]{1,63}$`, unique within a space;
- `name`: 1-120 characters;
- `description`: at most 500 characters;
- `category`: at most 80 characters;
- `unit`: at most 32 characters;
- `source_label`: at most 120 characters;
- `owner_label`: at most 120 characters;
- `freshness_minutes`: integer from 1 through 525,600;
- `warning_min`, `warning_max`, `critical_min`, and `critical_max`: optional decimal thresholds;
- `display_order`: bounded integer used for stable dashboard ordering;
- `status`: `active` or `archived`;
- `created_at` and `updated_at`: server timestamps.

Threshold semantics are deliberately data-only:

- a value below `critical_min` or above `critical_max` is critical;
- otherwise, a value below `warning_min` or above `warning_max` is warning;
- otherwise it is healthy;
- absent bounds are ignored; and
- lower bounds must satisfy `critical_min <= warning_min`, while upper bounds must satisfy `warning_max <= critical_max` when both are present.

Archiving hides a definition from the default dashboard but preserves its history. There is no ordinary hard-delete endpoint. Privacy erasure and retention are the only hard-deletion paths.

### KPI snapshot

Each snapshot has:

- `id`: server-generated opaque identifier;
- `account_id`, `space_id`, and `kpi_id`: mandatory ownership boundary;
- `value`: finite decimal stored as `NUMERIC(38,10)` in Postgres;
- `observed_at`: timezone-aware source observation time, normalized to UTC;
- `received_at`: server-controlled receipt time;
- `source_ref`: optional source-side reference limited to 200 characters;
- `idempotency_key`: caller-provided identifier limited to 128 characters;
- `created_by`: authenticated principal identifier, not caller supplied.

Snapshots are immutable except when privacy or retention rules delete them. Arbitrary JSON metadata is excluded to prevent secret leakage, unbounded payloads, and accidental personal-data storage.

The database enforces uniqueness for `(account_id, idempotency_key)` and `(kpi_id, observed_at)`. An exact retry returns the stored result. Reuse with different KPI, timestamp, value, or source reference returns `409 Conflict`. Historical backfill is allowed, but observations more than five minutes in the future are rejected.

## Authorization model

### Human reads

A KPI reader must:

- be an authenticated human principal;
- be pinned to the requested account tenant;
- be the account owner or hold an active membership whose `space_id` is empty (account-wide) or matches the requested space;
- request an active space owned by that account; and
- pass `check_app_access(account_id, "kpi_dashboard", space_id, "kpi_read")`.

Unknown and unauthorized object identifiers return the same not-found response where practical to avoid account, space, or KPI enumeration.

### Human configuration

Create, update, and archive operations additionally require account-admin authorization and the installed `kpi_configure` purpose for the exact space. Manual snapshot submission requires account-admin authorization and `kpi_snapshot_write` for the exact space.

Request models use explicit fields with unknown fields forbidden. IDs, ownership fields, status timestamps, and creator identity cannot be mass-assigned.

Browser mutations continue to use the existing host-only, HTTP-only, `SameSite=Lax` session cookie through the same-origin Next.js proxy. The KPI routes do not add cross-origin credential support, and no state change is exposed through `GET`.

### Service ingestion

The ingestion endpoint requires all of the following:

- a valid, active service key;
- the write scope;
- an account-pinned key;
- `app_id == "kpi_dashboard"` derived from the key;
- `kpi_snapshot_write` in the key's purposes;
- the requested space in the key's allowed spaces;
- an active KPI Dashboard installation granting `kpi_snapshot_write` for that space; and
- every referenced definition to belong to the derived account and requested space.

The request does not accept `account_id`, `app_id`, `purpose`, or creator fields. Those values are derived from the authenticated key and server constants. Object authorization is repeated for every referenced KPI rather than trusting possession of an identifier.

Provisioning continues to grant the KPI Dashboard installation all three purposes, but an automatically minted external credential is narrowed to write scope and `kpi_snapshot_write` only. Human sessions handle configuration. A separate key must be minted deliberately if a future trusted service requires read access.

## API contracts

### Workspace discovery

`GET /api/kpis/workspaces`

Returns only account/space pairs that the current human can read and for which KPI Dashboard is actively installed. Each result includes account and space labels plus `can_configure` and `can_write_manual` flags. It never returns service-key details.

### Dashboard summary

`GET /api/kpis?account_id=...&space_id=...&history_limit=30`

Returns active definitions in display order. Each definition contains its latest snapshot, previous snapshot, server-computed delta, freshness state, threshold state, and up to `history_limit` ordered points. The default is 30 and the maximum is 366. Archived definitions are included only when an authorized administrator explicitly requests them.

### Definition administration

- `POST /api/kpis` creates a definition.
- `PATCH /api/kpis/{kpi_id}` updates editable configuration or archives/restores the definition.
- `GET /api/kpis/{kpi_id}/snapshots` returns cursor-bounded history for setup and inspection.

Definition operations carry `account_id` and `space_id` so authorization can be applied before and after object lookup. History requests have an upper limit of 366 points per page and deterministic `(observed_at, id)` ordering.

### Manual setup snapshot

`POST /api/kpis/{kpi_id}/snapshots`

Allows an authorized account administrator to submit one value through the UI. It uses the same validation, idempotency, immutability, future-time guard, and audit path as machine ingestion.

### Machine ingestion

`POST /api/service/kpis/snapshots`

The body contains one `space_id` and between 1 and 100 snapshot items. Each item contains only `kpi_key` or `kpi_id`, `value`, `observed_at`, `source_ref`, and `idempotency_key`. Supplying both key and ID, or neither, is invalid.

The batch is all-or-nothing. Validation or a conflicting replay rolls back the complete batch. Exact duplicates are returned as duplicates without new rows. The response reports accepted and duplicate counts plus snapshot IDs; it does not echo secrets or internal key material.

## Persistence and migrations

Migration `0023_kpi_dashboard_data` will create:

- `kpi_definitions`;
- `kpi_snapshots`;
- foreign keys to platform account and space records;
- the definition-key and snapshot-idempotency uniqueness constraints;
- an index on `(account_id, space_id, status, display_order)`;
- an index on `(kpi_id, observed_at DESC, id DESC)`; and
- deletion behavior that removes snapshots when a definition is removed by a governed hard-delete path.

Both KPI tables will be added to required schema validation and the required Alembic revision will advance to `0023_kpi_dashboard_data`.

Both tables will be added to the RLS-required table list. They will use the existing transaction-local account and space settings, with row security enabled and forced. Policies will deny access when the account scope is unset or does not match and will additionally enforce space scope when one is set. The migration tests will verify policy creation, forced RLS, grants, indexes, constraints, and downgrade symmetry.

The Postgres store will use parameterized SQL exclusively. Batch ingestion will resolve definitions and insert accepted rows in one transaction. No partial commit is allowed.

The memory store will persist to a separate `kpis.json` with thread-safe operations and the same domain behavior as its Postgres counterpart. A malformed KPI file must not clear or alter platform governance persistence.

## Resource and performance controls

- Global body-size middleware remains the outer request limit.
- Ingestion models allow at most 100 fixed-shape items and forbid extra fields.
- The existing per-service-key rate limiter applies before KPI ingestion.
- History limits and archive inclusion are server bounded.
- A space may hold at most 500 definitions, of which at most 250 may be active, keeping the summary response bounded while leaving room for archived configuration.
- Values are finite decimals with at most ten fractional digits and an absolute value below `10^28`, matching `NUMERIC(38,10)`.
- Strings have explicit maximum lengths.
- Ingestion uses one transaction per batch.
- Dashboard summary is one store operation, not one request or query per KPI tile.
- Latest and previous values are selected with indexed set-based SQL.
- History is bounded before serialization.
- Private KPI responses retain `no-store` behavior through the existing Next.js proxy.
- No external API call occurs in a dashboard request.

These controls bound database work, response size, memory use, and storage growth per request. The existing in-process limiter is appropriate for the current single-API-instance customer deployment model. A distributed limiter is required before horizontally scaling a customer API across multiple independent processes.

## Auditing and observability

Definition creation, modification, archive/restore, manual snapshots, and service ingestion produce platform audit events. Snapshot ingestion records one event per batch, containing:

- service-key or human principal ID;
- account and space;
- KPI IDs;
- accepted and duplicate counts;
- success or rejection outcome; and
- a server-generated request correlation identifier.

Audit events never contain KPI values, bearer tokens, raw request bodies, or unrestricted source data. Validation failures and rate-limit events use sanitized, bounded messages. Service-key usage is recorded under a specific KPI ingestion operation.

## Privacy, retention, and legal holds

KPI data is treated as customer content even though the intended inputs are aggregate business metrics.

- Privacy export includes KPI definitions and snapshots in the requested account/space.
- Account or space erasure deletes matching KPI snapshots and definitions.
- A legal hold covering the account or space blocks KPI deletion through the existing hold gate.
- Retention policies can target the KPI domain; dry runs count eligible rows and real runs delete only provably old snapshots. Age is calculated from the server-controlled `received_at`, never the caller-controlled observation time.
- Definitions are removed only for account/space erasure, not merely because their snapshots age out.
- Deletion counts include KPI definitions and snapshots.
- Existing append-only security audit behavior remains unchanged and stores no metric values.

The UI and ingestion documentation state that snapshots are for aggregate KPIs and must not use person names, email addresses, or subject identifiers as KPI keys or source references.

## Web application

The `/kpis` page remains session protected. Its client will:

1. load authorized KPI workspaces;
2. select the first valid workspace or preserve a valid URL selection;
3. request one dashboard summary;
4. render loading, error, empty, and populated states honestly; and
5. expose management controls only when the server reports configuration authority.

The live dashboard shows:

- KPI name, category, owner, unit, and source;
- current value and observation time;
- absolute change when a previous value exists and percentage change only when the previous value is non-zero;
- fresh, stale, warning, critical, or awaiting-data state;
- an accessible bounded history visualization; and
- workspace-level counts for active, stale, warning, critical, and awaiting-data KPIs.

The management area supports definition creation/editing, archive/restore, and one manual test snapshot. Forms provide field-level validation and keep the same action wording through success or failure messages.

There is no sample-data fallback. An API failure shows a retryable error. An empty workspace explains how to create the first KPI and provides a concise ingestion example after configuration.

The client uses the existing Next.js API proxy and typed client module. Independent requests are parallelized where possible; data that depends on the selected workspace remains sequential. Derived counts are computed from the single summary response rather than stored as duplicate React state.

## Error behavior

- `400` for malformed scopes, invalid threshold relationships, invalid timestamps, or ambiguous KPI references.
- `401` for missing or invalid authentication.
- `403` for authenticated callers lacking a required role, key scope, or fixed function grant.
- `404` for missing or unauthorized account, space, or KPI objects when returning `403` would reveal object existence.
- `409` for definition-key collisions or conflicting idempotency replays.
- `413` for requests exceeding the global body limit.
- `422` for typed field validation failures.
- `429` with `Retry-After` when the service-key rate limit is exceeded.

Responses expose concise client-safe messages and never include stack traces, SQL details, secret material, or cross-tenant identifiers.

## Testing strategy

### Domain and stores

- Definition validation, threshold ordering, archive behavior, and status evaluation.
- Finite decimal validation and UTC normalization.
- Latest, previous, delta, freshness, and bounded-history calculation.
- Exact retry behavior and conflicting replay rejection.
- All-or-nothing batch semantics.
- Account/space filtering in memory and Postgres stores.
- JSON persistence isolation from the platform store.

### Authorization and API

- Anonymous access fails closed.
- Members can read only authorized KPI workspaces.
- Non-admin members cannot configure or submit manual values.
- Admins of one account cannot enumerate or mutate another account's KPIs.
- Changing a KPI ID, key, account, space, HTTP method, app ID, or purpose cannot cross the authorization boundary.
- Service keys must be active, account-pinned, KPI-app-pinned, space-enabled, purpose-enabled, and write-scoped.
- Request ownership fields and unknown fields cannot be mass-assigned.
- Oversized batches, excessive history limits, future timestamps, and rate-limit overflow are rejected.
- Exact batch retries are idempotent and conflicting retries return `409`.
- Audit events omit values and secrets.

### Database safety

- Migration upgrade and downgrade structure.
- Both KPI tables have enabled and forced RLS.
- Runtime-role reads and writes fail with unset or mismatched RLS scope.
- Required-schema validation fails on a pre-0023 database.
- Unique constraints and indexes match the contract.

### Privacy

- Export includes only the requested KPI scope.
- Erasure removes only the requested KPI scope.
- Retention dry-run and deletion counts are correct.
- Legal holds prevent KPI deletion.

### Web client

- Typed request/response contracts compile.
- Loading, access-denied, error, empty, and populated states render correctly.
- Management controls follow server authority flags.
- Refresh uses live data and never restores samples.
- Responsive layout, keyboard focus, accessible labels, and reduced-motion behavior are preserved.
- Frontend lint and production build pass.

## Documentation

The repository documentation will include:

- the definition and snapshot concepts;
- a safe `curl` ingestion example using placeholder credentials;
- idempotency and replay semantics;
- service-key scope and purpose requirements;
- batch, rate, precision, and timestamp limits;
- privacy guidance forbidding person-level identifiers; and
- the boundary between the generic ingestion API and future vendor connectors.

## Existing integration corrections

The implementation also corrects the existing platform app-install request validation so `kpi_dashboard` is an accepted app ID. Provisioning tests will be updated to prove that the installed app retains `kpi_read`, `kpi_configure`, and `kpi_snapshot_write`, while its automatically minted ingestion credential is narrowed to the write scope and `kpi_snapshot_write` purpose.

## Delivery boundary

The implementation is delivered only when relevant Python tests, migration checks, frontend lint, frontend production build, and repository safety checks pass. Shipping follows the repository workflow: stage task-only files, commit, push the feature branch, fast-forward local `main`, merge the feature branch, and push `main`. Shipping stops if checks fail, unrelated changes are present, secrets are detected, or a merge conflict occurs.

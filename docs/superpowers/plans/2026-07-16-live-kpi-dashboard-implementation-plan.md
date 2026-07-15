# Live KPI Dashboard Implementation Plan

**Date:** 2026-07-16
**Status:** Approved design; ready for implementation
**Design:** `docs/superpowers/specs/2026-07-16-live-kpi-dashboard-design.md`

## Objective

Deliver the approved ingestion-first KPI system: persisted KPI definitions and immutable snapshot history, hardened human and service APIs, privacy and retention integration, and a live dashboard with administration controls.

## Safety invariants

- Account and app identity for machine ingestion come from the authenticated service key, never the body.
- Every operation rechecks account, space, app purpose, and referenced KPI ownership.
- Machine ingestion accepts only KPI-app-pinned, write-scoped keys with `kpi_snapshot_write`.
- Snapshot payloads are fixed shape, bounded to 100 items, decimal-only, timestamp-validated, and transactionally idempotent.
- KPI Postgres tables have enabled and forced RLS plus account/space policies.
- KPI values and secrets never enter platform audit metadata.
- Retention age uses server-controlled receipt time.
- The dashboard never labels sample data as live data and never falls back to samples on errors.

## UI design direction

**Subject:** an executive operating ledger for an organization's current business signals.

**Audience:** authorized executives and account administrators.
**Single job:** reveal which business metrics changed, are stale, or require action without querying each source system.

### Tokens and type

- Ink `#16191e`: navigation, headings, and high-confidence values.
- Paper `#f4f2ee`: established OneBrain workspace background.
- Surface `#ffffff`: ledger and form surfaces.
- Copper `#a66e2f`: selected scope and observation markers.
- Healthy `#1f7a4d`: in-range values.
- Critical `#b4453e`: breached thresholds.

Use the existing system UI stack for interface and headings, with restrained heavy weights. Use the existing `SFMono-Regular`/Consolas utility stack for metric values, timestamps, and source keys so operational data reads like an instrument rather than marketing copy.

### Layout

```text
+------------------------------------------------------------------+
| KPI ledger       workspace / freshness window       Add KPI       |
+------------------------------------------------------------------+
| Active | Stale | Warning | Critical | Awaiting data               |
+------------------------------------------------------------------+
| METRIC / SOURCE | CURRENT | DELTA | HEALTH | OBSERVATION HORIZON  |
| Cash runway     | 14.2 mo | +1.1  | healthy| ▁▂▃▅▆  12 min       |
| Gross margin    | 68 %    | +3.0  | warning| ▅▅▆▇▇  18 min       |
+------------------------------------------------------------------+
| Definition editor / manual setup value (authorized admins only)   |
+------------------------------------------------------------------+
```

The signature element is the observation horizon: an accessible compact chart integrated into each ledger row, ending in a visible latest-observation marker. The page avoids a generic hero and repeated card grid. On mobile, each ledger row becomes one stacked metric record while preserving the same information order.

The plan keeps motion to a single restrained initial ledger reveal and chart draw, disabled under reduced-motion preferences. It reuses the established palette and shell so the feature remains recognizably OneBrain.

## Implementation order

### 1. Add KPI domain models and validation

**Files**

- Add `app/kpis/__init__.py`.
- Add `app/kpis/base.py`.
- Add `tests/test_kpis.py`.

**Test first**

- Definition keys, lengths, freshness, status, definition-count limits, and threshold ordering validate exactly as specified.
- Decimal values reject NaN, infinity, excess precision, and magnitude outside `NUMERIC(38,10)`.
- Observation timestamps require time zones, normalize to UTC, and reject more than five minutes in the future.
- Threshold, freshness, absolute delta, and non-zero percentage delta states are deterministic.
- Snapshot equality used for replay checks compares normalized semantic fields.

**Implement**

- Frozen dataclasses for `KpiDefinition` and `KpiSnapshot`.
- Status/result dataclasses for dashboard summaries and batch ingestion.
- Pure normalization and validation helpers with no store or network dependency.
- Server-time injection in functions that need time so tests do not depend on the wall clock.

### 2. Build the memory store and contract

**Files**

- Add `app/kpis/memory.py`.
- Extend `app/kpis/base.py` with the store protocol.
- Modify `tests/test_kpis.py`.

**Test first**

- Definitions are unique by account, space, and key.
- At most 500 total and 250 active definitions exist per space.
- Create/update/archive stays inside the original account and space.
- Snapshot batches are all-or-nothing.
- Exact idempotency and timestamp duplicates return existing records; conflicting duplicates fail.
- Dashboard summaries return ordered definitions with latest, previous, bounded history, delta, freshness, and threshold state.
- Export, retention count/delete by `received_at`, and account/space hard deletion are isolated.
- Persistence reloads `kpis.json`; malformed KPI persistence does not touch platform state.

**Implement**

- Thread-safe dictionaries with a separate JSON persistence path.
- Copy-on-validate batch writes so a failed item cannot partially mutate memory.
- Stable ordering by display order/name and observation time/ID.
- Store methods matching the minimum API, privacy, and retention requirements.

### 3. Add Postgres schema, RLS, and store

**Files**

- Add `migrations/versions/0023_kpi_dashboard_data.py`.
- Add `app/kpis/postgres.py`.
- Add `app/kpis/factory.py`.
- Modify `app/db/schema.py`.
- Modify `app/db/rls.py`.
- Modify `app/deps.py`.
- Modify `tests/test_postgres_schema_validation.py`.
- Extend `tests/test_kpis.py` with Postgres store query-contract tests where the repository uses fakes.

**Test first**

- Alembic head advances to `0023_kpi_dashboard_data` with the correct down revision.
- Upgrade creates both tables, checks, foreign keys, unique constraints, and indexes.
- Both tables enable and force RLS with account/space `USING` and `WITH CHECK` policies.
- Downgrade removes only KPI-owned schema and policies.
- Required-schema and RLS validation include both tables.
- Store SQL always sets account/space scope and uses parameters.
- Batch ingestion commits once and rolls back completely on conflict.
- Latest/previous/history summary uses bounded set-based retrieval.

**Implement**

- `kpi_definitions` and `kpi_snapshots` using the approved columns and `NUMERIC(38,10)`.
- Runtime grants matching existing customer-scoped tables.
- Postgres row adapters and store methods equivalent to the memory backend.
- A KPI store factory selecting Postgres for `pgvector` deployments and `kpis.json` otherwise.
- Cached dependency wiring in `app.deps`.

### 4. Harden account-member and KPI authorization

**Files**

- Modify `app/auth/account_access.py`.
- Add `app/kpis/access.py`.
- Add `tests/test_kpi_api.py`.

**Test first**

- Account owners and active account-wide or matching-space members can read when the app grants `kpi_read`.
- Membership in a different space, revoked membership, wrong tenant, wrong app grant, and private/non-enabled space fail closed.
- Only account admins with `kpi_configure` can create or edit definitions.
- Only account admins with `kpi_snapshot_write` can submit manual points.
- Missing and unauthorized objects use equivalent not-found behavior when needed to prevent enumeration.

**Implement**

- A read helper that checks human principal type, tenant pin, ownership/membership, exact space, and KPI app access.
- Configuration and manual-write helpers layered on existing account-admin authorization.
- A referenced-definition resolver that always validates account/space ownership.

### 5. Implement human KPI APIs

**Files**

- Add `app/routers/kpis.py`.
- Modify `app/main.py`.
- Modify `tests/test_kpi_api.py`.

**Test first**

- Workspace discovery returns only readable KPI-enabled spaces and accurate authority flags.
- Summary defaults to 30 history points and caps at 366.
- Create/update models forbid extra and server-owned fields.
- Definition collisions and limit violations return `409`.
- Manual snapshots share validation and replay behavior with service ingestion.
- Archive/restore preserves history.
- Audit metadata contains identifiers/counts but no values or secrets.

**Implement**

- Pydantic v2 strict request/response models with `extra="forbid"`.
- Workspace, summary, create, patch, history, and manual-snapshot endpoints.
- Central exception-to-HTTP translation that keeps messages bounded and safe.
- One platform audit event for each completed configuration/manual action.

### 6. Implement service-key batch ingestion

**Files**

- Modify `app/routers/service.py`.
- Modify `app/auth/principal.py`.
- Modify `app/provisioning/service.py`.
- Modify `app/routers/platform.py`.
- Modify `tests/test_kpi_api.py`.
- Modify `tests/test_provisioning.py`.
- Modify `tests/test_service_keys.py` if usage-operation coverage belongs there.

**Test first**

- Missing/invalid/inactive keys fail authentication.
- Read-only, wrong-app, wrong-purpose, wrong-account, and wrong-space keys fail authorization.
- Body attempts to supply account/app/purpose/creator fields fail validation.
- Batch size is 1-100 and ambiguous ID/key references fail.
- App installation access is rechecked for the exact space.
- One transaction accepts new rows and returns exact duplicates.
- Conflicting replay returns `409` with no partial insert.
- Rate limit returns `429` and `Retry-After`.
- Key usage records `service.kpis.snapshots.write`.
- Provisioned KPI credentials contain only write scope and `kpi_snapshot_write`; the installation still has all KPI purposes.
- Manual app installation accepts `kpi_dashboard`.

**Implement**

- `/api/service/kpis/snapshots` under the existing service router.
- Reuse the existing write-scope and per-key rate-limit helpers.
- Derive account/app/principal identity from the service key.
- Resolve all definitions before a single batch-store call.
- Record one sanitized audit event per accepted or application-rejected batch.
- Narrow only the automatically minted KPI integration credential.
- Add `kpi_dashboard` to platform app-install request validation.

### 7. Integrate privacy and retention

**Files**

- Modify `app/routers/privacy.py`.
- Modify `app/retention/service.py`.
- Modify `tests/test_privacy.py`.
- Modify `tests/test_retention.py`.

**Test first**

- Privacy export includes only matching KPI definitions and snapshots.
- Account erasure removes all account KPI data; space erasure leaves other spaces intact.
- KPI deletion counts appear in the response and audit metadata.
- Legal holds block KPI deletion with no partial removal.
- `kpis` is a supported retention domain.
- Retention eligibility uses `received_at`, not caller-controlled `observed_at`.
- Dry runs count without deleting; real runs delete only eligible snapshots and preserve definitions.

**Implement**

- Add KPI content to privacy export output.
- Delete KPI scope inside the existing legal-hold-guarded erase flow.
- Add KPI snapshot retention paths and counts.
- Keep definitions outside ordinary age-based retention.

### 8. Add typed web-client contracts

**Files**

- Modify `onebrain-web/src/lib/onebrain-types.ts`.
- Modify `onebrain-web/src/lib/onebrain-client.ts`.
- Modify `onebrain-web/src/components/workspace-provider.tsx` only if KPI workspace discovery cannot reuse its selected scope cleanly.

**Verify while implementing**

- KPI types mirror API responses without `any`.
- One client call retrieves each summary; no per-tile calls exist.
- Query strings are constructed through `URLSearchParams`.
- Mutation helpers send only editable fields.
- Errors retain server detail without leaking raw response bodies.

**Implement**

- Types for workspaces, definitions, snapshots, dashboard rows, inputs, and ingestion-independent admin mutations.
- Client methods for workspaces, summary, create, patch, history, and manual snapshot.
- Reuse the existing `/api/onebrain` same-origin proxy and `no-store` behavior.

### 9. Build the live operating-ledger dashboard

**Files**

- Replace `onebrain-web/src/components/kpi-panel.tsx`.
- Modify `onebrain-web/src/app/globals.css`.

**Verify while implementing**

- Loading, access denied, API failure, no workspace, empty workspace, awaiting data, and live-data states are explicit.
- No hard-coded KPI values remain.
- Workspace changes cancel or ignore stale requests.
- One summary response derives all counts without effect-driven duplicate state.
- Admin controls follow server flags.
- Definition form validates thresholds and required fields before sending.
- Manual value entry requires an idempotency key generated from KPI/time for a deliberate test submission.
- Charts have accessible labels and do not rely on color alone.
- Keyboard focus, mobile layout, and reduced motion work.

**Implement**

- Header with current workspace, refresh, and authorized Add KPI action.
- Operational count strip derived from summary rows.
- Responsive KPI ledger with current, delta, state, freshness, and inline SVG/CSS history horizon.
- Empty/error guidance with precise recovery actions.
- Admin editor for create/update/archive and a manual setup-value form.
- Restrained existing-brand styling following the UI design direction above.

### 10. Document, verify, and ship

**Files**

- Add `docs/kpi-ingestion.md`.
- Modify `docs/README.md`.
- Modify `README.md` only if the top-level feature list needs the new contract linked.

**Documentation**

- Explain definitions, snapshots, immutability, and aggregate-data guidance.
- Provide a placeholder-token `curl` example with an idempotency key.
- Document scope/purpose requirements, batch limits, precision, timestamps, retries, and error codes.
- State that vendor-specific jobs call this API and keep vendor credentials outside OneBrain.

**Focused verification**

```powershell
py -m pytest -q tests/test_kpis.py tests/test_kpi_api.py tests/test_provisioning.py tests/test_privacy.py tests/test_retention.py tests/test_postgres_schema_validation.py tests/test_service_keys.py
npm run typecheck --prefix onebrain-web
npm run lint --prefix onebrain-web
npm run build --prefix onebrain-web
```

**Full verification**

```powershell
py -m pytest -q
git diff --check
```

Review changed files for accidental credentials, raw bearer tokens, real customer data, and unrelated edits. If clean, stage only task files, commit, push the feature branch, switch to `main`, pull with `--ff-only`, merge the feature branch, and push `main` according to `AGENTS.md`.

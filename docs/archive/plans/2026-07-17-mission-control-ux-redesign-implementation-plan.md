# Mission Control UX redesign implementation plan

**Date:** 2026-07-17

**Design:** `docs/archive/specs/2026-07-17-mission-control-ux-redesign-design.md`

## Objective

Replace Mission Control's preset-bundle provisioning and dense technical
screens with a development-only Core-plus-modules model and a clear operator
experience. The implementation must make the current condition, next action,
and exact time visible before technical detail, while retaining that detail
behind explicit Expand controls.

## Delivery constraints

- Work in an isolated `codex/mission-control-ux-redesign` worktree created
  from this committed design/plan baseline. The current worktree has unrelated
  in-progress production-hardening changes that must not be staged, reset,
  merged, or overwritten.
- This is intentionally breaking for the development project: remove preset
  bundle endpoints, fields, types, persistence, and tests rather than adding a
  compatibility adapter.
- OneBrain Core is server-enforced. The only selectable optional module IDs are
  `assistant`, `kpi_dashboard`, `ai_employees`, and `communication`.
- Retain raw infrastructure/provider data only in expanded detail. Never invent
  a timestamp or treat absent telemetry as healthy.

## Implementation order

### 1. Establish the module catalogue and composition contract

**Files**

- Replace `app/provisioning/bundles.py` with a module-catalogue/composition
  module, or rename it to a module-oriented name and update imports.
- Modify `app/provisioning/service.py`.
- Rewrite the bundle-focused portions of `tests/test_provisioning.py`.

**Test first**

- Core-only selection resolves OneBrain Core's spaces, app, and deployable
  services.
- Each optional module resolves its required spaces, app installation, purposes,
  and deployable services.
- Mixed selections produce a deterministic, de-duplicated union in catalogue
  order.
- Empty selection is valid; unknown or duplicate optional module IDs fail.
- KPI Dashboard and AI Employees remain selected/deployed apps even though they
  add no separate container service today.

**Implement**

- Define a server-owned optional-module catalogue using the existing core,
  assistant, communication, KPI, and AI Employee templates/purposes.
- Resolve a `ModuleComposition` containing selected optional IDs, spaces, app
  templates, and deployable service IDs. Core is implicit and cannot be removed
  by a browser payload.
- Change `CustomerProvisioner.provision` and `ProvisioningResult` from
  `bundle_id`/`ProvisioningBundle` to the resolved composition and selected
  `module_ids`.
- Validate module-version overrides against resolved deployable service IDs and
  app-theme overrides against resolved app IDs.
- Record selected optional IDs, resolved modules, and issued-key IDs in the
  provisioning audit event.

### 2. Replace bundle persistence with explicit selected-module state

**Files**

- Use the implemented `migrations/versions/0025_provisioning_module_selection.py`;
  the subsequent hardening chain begins at `0026_job_leases.py`.
- Modify `app/controlplane/base.py`, `app/controlplane/memory.py`, and
  `app/controlplane/postgres.py`.
- Modify `app/provisioning/runs.py`.
- Modify `app/db/schema.py`.
- Update `tests/test_provisioning_runs.py`, `tests/test_controlplane.py`, and
  `tests/test_postgres_schema_validation.py`.

**Test first**

- `CustomerDeployment.selected_module_ids` and `ProvisioningRun.module_ids`
  round-trip through the memory and Postgres stores.
- A migration removes `provisioning_runs.bundle_id`, adds JSON module IDs to
  provisioning runs, and adds JSON selected-module IDs to deployments.
- Deployment/service rows remain the resolved container modules, while selected
  optional IDs retain the product choices such as KPI Dashboard and AI
  Employees.

**Implement**

- Add `selected_module_ids` to `CustomerDeployment` and every memory/Postgres
  deployment mapper, query, and insert.
- Replace `ProvisioningRun.bundle_id` with `module_ids` in dataclasses, memory
  JSON serialization, Postgres columns/row mapping, insert/update/returning
  queries, and `create_run` calls.
- Add the development schema migration that drops `bundle_id`, adds the two
  JSONB fields with empty-list defaults, and updates the required Alembic head.
- Store the selected IDs in provisioning-run request payloads so retry is
  reproducible.

### 3. Replace the provisioning API and development-gate dependency

**Files**

- Modify `app/routers/provisioning.py`.
- Modify `app/routers/operator.py`.
- Modify `app/provisioning/hetzner/provisioner.py` and related fixtures only
  where they construct or consume `ProvisioningRun`.
- Update `tests/test_provisioning.py`, `tests/test_hetzner_provisioner.py`, and
  `tests/test_release_promotion.py`.
- Update or retire the legacy static provision form in `app/static/index.html`
  and `app/static/js/operator.js` so it never calls a removed bundle endpoint.

**Test first**

- `GET /api/provisioning/modules` returns Core information and the four
  selectable optional modules; `GET /bundles` no longer exists.
- `CustomerProvisionCreate` accepts `module_ids`, rejects duplicate/unknown
  choices, and returns selected IDs and resolved installation/modules.
- Provisioning runs and retries expose `module_ids`, never `bundle_id`.
- The development gate uses one explicit fixed optional-module selection and
  derives its required service images from the same composition code.

**Implement**

- Replace `BundleOut` and the bundle-list endpoint with a module-catalogue DTO.
- Replace all request/result/run `bundle_id` fields and serialization with
  `module_ids`.
- Change provision/retry payload construction and result mapping to carry the
  selected IDs through API, service, run store, and Hetzner dispatch.
- Remove `DEVELOPMENT_GATE_BUNDLE_ID`; use a fixed development-gate selection
  resolved by the module catalogue.
- Keep server validation as the source of truth; the browser never decides core
  service requirements.

### 4. Surface complete operator lifecycle data and sort newest first

**Files**

- Modify `app/routers/operator.py`.
- Modify `app/controlplane/memory.py` and `app/controlplane/postgres.py`.
- Modify `app/controlplane/pull_reconcile.py` only if backup occurrence time is
  currently lost at Postgres persistence.
- Update `tests/test_controlplane.py`, `tests/test_rollout_exec.py`,
  `tests/test_release_promotion.py`, and any router response tests.

**Test first**

- Backup and health API responses include `created_at` and detail.
- Rollout responses include creation, dispatch, completion, execution status,
  safe provider/run metadata, failure reason, and URL where available.
- Releases return `created_at DESC, version DESC` in memory and Postgres.
- Deployment rollouts are newest first without changing the active-rollout
  concurrency rule.
- A reported backup time is not silently replaced by its later receipt time.

**Implement**

- Extend `BackupOut`, `HealthOut`, and `RolloutOut` plus their mapper helpers
  with the already-persisted lifecycle fields.
- Standardize `list_releases` and user-facing rollout list ordering.
- Preserve existing deployment, provisioning, promotion, fleet, and
  observability timestamps; expose only safe operational metadata.
- Ensure customer readiness remains raw/safe at the API boundary and is
  translated into plain language in shared UI helpers.

### 5. Use one canonical AI employee record

**Files**

- Modify `app/routers/ai_employees.py`.
- Update `tests/test_ai_employee_api_scope.py` and, where needed,
  `tests/test_ai_employee_contracts.py`.

**Test first**

- Team and employee-detail responses include safe actions, approval rule,
  never-without-approval list, productivity metrics, description, and existing
  identity/profile fields.
- No hidden prompts, credentials, or authority policy leaks into the response.

**Implement**

- Extend the existing canonical DTO/mapping from
  `app/ai_employees/contracts.py`; do not create a second static roster for
  Mission Control.

### 6. Regenerate the web contract and add pure operational helpers

**Files**

- Modify `onebrain-web/src/lib/onebrain-types.ts` and
  `onebrain-web/src/lib/onebrain-client.ts`.
- Regenerate `onebrain-web/src/lib/openapi.json` using
  `python scripts/export_openapi.py onebrain-web/src/lib/openapi.json`.
- Add `onebrain-web/src/lib/operational.ts`.
- Add Node tests under `onebrain-web/tests/`.

**Test first**

- Timestamp formatting handles local date/time, relative age, invalid values,
  and no-signal language.
- Status mapping returns a clear condition, explanation, and next action for
  healthy, updating, failed, pending, and missing telemetry.
- Module selection always includes Core conceptually, keeps optional IDs unique,
  and selects the newest eligible release explicitly.
- Tab-count helpers count actual runs/rollouts rather than deployments.

**Implement**

- Remove bundle types/client calls; add module-catalogue and `module_ids`
  contracts.
- Extend backup, health, rollout, deployment, fleet, provisioning, release,
  and employee types with the approved API data.
- Centralize deterministic formatting/sorting/status language outside React
  components.

### 7. Add shared operational UI primitives and styles

**Files**

- Add focused components under `onebrain-web/src/components/operational/`:
  `timestamp.tsx`, `status-summary.tsx`, and `expandable-card.tsx`.
- Modify `onebrain-web/src/app/globals.css`.

**Test first**

- The primitives render explicit no-signal content, a semantic `time` element,
  readable relative age, and accessible expanded/collapsed controls.

**Implement**

- Keep the visual language compact and calm: one status, a sentence explaining
  it, a next action, and a timestamp before diagnostics.
- Add responsive layouts for module cards, operational cards, compact timelines,
  and expanded detail without reintroducing the current wall of data.

### 8. Split and rebuild the Control experience

**Files**

- Refactor `onebrain-web/src/components/operator-panel.tsx` into focused
  components under `onebrain-web/src/components/operator/`.
- Add at least `provisioning-wizard.tsx`, `module-selector.tsx`,
  `release-ring-help.tsx`, `provisioning-review.tsx`, `customer-card.tsx`,
  `provisioning-run-ledger.tsx`, `release-timeline.tsx`, and
  `rollout-card.tsx`.

**Test first**

- The wizard has Customer details -> Modules -> Release -> Review stages.
- Core is visibly fixed; optional cards are selectable; no bundle dropdown is
  rendered.
- No eligible version names the missing module image beside Initial version.
- Customer cards default collapsed and reveal diagnostics/keys only after
  Expand.
- Provisioning, Releases, and Rollouts labels communicate what their counts
  represent.

**Implement**

- Keep `OperatorPanel` as the data-fetch/action coordinator and move rendering
  to bounded components.
- Make the fixed Hetzner development type and Nuremberg region visible but
  read-only.
- Add a release-ring information control explaining Manual, Internal, Pilot,
  Early, and Stable.
- Replace raw `backup success`, `health none`, and `version / pending` strings
  with `StatusSummary`, `Timestamp`, and an Expand section.
- Render releases newest first as stage timelines with all reached stage times,
  next action, and expandable audit history.
- Render rollout cards with lifecycle times, failures, and provider diagnostics
  behind Expand.

### 9. Rebuild Status, employee summary, Fleet, and navigation

**Files**

- Modify `onebrain-web/src/components/cockpit-panel.tsx`.
- Modify `onebrain-web/src/components/ai-employee-organization.tsx` and, if
  useful, extract a reusable canonical employee directory.
- Modify `onebrain-web/src/components/ai-employees-panel.tsx` only for the
  shared canonical employee data/expanded detail path.
- Modify `onebrain-web/src/components/fleet-panel.tsx`.
- Modify `onebrain-web/src/components/console-shell.tsx`.

**Test first**

- Status displays observability `generated_at` as "Refreshed at" and selects a
  clear highest-priority action.
- Every employee is visible in a compact card; expanded detail comes from the
  canonical API, not a fictional local list.
- Fleet versions/heartbeats/rollouts show local timestamps and explicit empty
  states.
- Mission Control exposes Settings; no non-clickable `all locations` footer
  remains.

**Implement**

- Remove the static eight-person dossier from `CockpitPanel`.
- Use the employee directory for compact cards with explicit Expand controls.
- Apply operational primitives to Fleet's date-only/hidden rollout information.
- Add a clear Settings/account path and Logout; remove redundant noninteractive
  role/location text. Use session identity rather than hard-coded account text.

### 10. Validate and hand off safely

**Focused checks**

```powershell
python -m pytest tests/test_provisioning.py tests/test_provisioning_runs.py tests/test_hetzner_provisioner.py -q
python -m pytest tests/test_controlplane.py tests/test_release_promotion.py tests/test_rollout_exec.py -q
python -m pytest tests/test_ai_employee_api_scope.py tests/test_postgres_schema_validation.py -q
npm --prefix onebrain-web test
npm --prefix onebrain-web run typecheck
npm --prefix onebrain-web run lint
npm --prefix onebrain-web run build
python scripts/export_openapi.py onebrain-web/src/lib/openapi.json
git diff --check
```

**Manual verification**

- Use the development Mission Control UI to provision Core-only, one optional
  module, and a multi-module customer.
- Verify no bundle vocabulary remains in provisioning or operator cards.
- Verify a healthy, missing-signal, and failed rollout card each name what
  happened, what to do next, and when it was last updated.
- Verify the employee directory, customer details, release history, and rollout
  diagnostics stay collapsed until explicitly expanded.
- Verify desktop and narrow/mobile layouts, keyboard expansion, Settings, and
  Logout.

**Shipping discipline**

- Commit only files changed in the isolated UX worktree.
- Do not merge or push the UX branch into `main` while the separate
  production-hardening worktree is dirty or unreviewed. Reconcile the two
  intentionally first.

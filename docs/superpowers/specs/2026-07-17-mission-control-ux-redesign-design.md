# Mission Control UX redesign

**Date:** 2026-07-17
**Status:** approved for implementation

## Purpose

Mission Control is currently a development-only control surface. It must help an
operator answer four questions immediately:

1. What needs attention now?
2. What happened, and when?
3. What should happen next?
4. Where can I safely see the technical detail?

The redesign replaces dense technical dumps, fixed provisioning bundles, and
date-only operational history with clear summaries, expandable detail, module
selection, and consistent timestamps.

## Scope and constraints

- This is a development project with one development server. There is no need
  to preserve the existing preset-bundle API or data shape.
- OneBrain Core is mandatory for every new customer.
- The supported optional modules are Assistant, KPI Dashboard, AI Employees,
  and Communication.
- Technical identifiers and raw provider language remain available in expanded
  detail, but are never the primary status message.
- All displayed operational times use the operator's local date and time,
  include a relative age, and retain an exact machine-readable `dateTime` value.

## Provisioning: core plus selected modules

### Domain model

Replace the fixed `ProvisioningBundle` catalogue and `bundle_id` contract with a
server-owned module catalogue. The server exposes the optional module choices
and enforces OneBrain Core internally; the browser never sends Core as an
optional choice.

The provisioning request contains `module_ids: string[]` rather than
`bundle_id`. It is valid to submit an empty list, which creates Core only. The
server validates module IDs, rejects duplicates and unknown values, resolves
the required spaces/apps/deployment services, and persists the selected module
IDs with the deployment and provisioning run. Existing preset-bundle code,
endpoints, types, tests, and `bundle_id` fields are removed rather than
translated.

The resulting customer/deployment still exposes its installed modules so
Control, Releases, and Rollouts can explain exactly what is deployed.

### Provisioning flow

The Provisioning tab becomes a short setup flow:

1. Customer details: customer name and owner email.
2. Modules: an always-selected OneBrain Core card and four optional module
   cards with checkboxes, descriptions, and a live selected-module summary.
3. Release: an eligible initial version and a release ring.
4. Review: the customer, selected modules, fixed development target, and
   chosen release before provisioning.

Deployment type and region stay visible as fixed development values. Release
ring has an accessible info control describing Manual, Internal, Pilot, Early,
and Stable. If there is no eligible version for the selected modules, the
Initial version field explains that in place, including which module images are
missing.

## Operational information design

### Shared status contract

Every operational card has the same hierarchy:

1. A plain-language condition: Healthy, Updating, Needs attention, Pending, or
   Not yet reported.
2. A short explanation of what caused that condition.
3. A specific next action when action is required.
4. A last-updated timestamp with local date/time and relative age.
5. An Expand control for diagnostics and identifiers.

New shared UI primitives provide this consistently:

- `Timestamp` renders a local date/time, relative age, and exact `dateTime`.
- `StatusSummary` converts raw backend states into clear language and an
  operator action.
- `ExpandableCard` controls collapsed/expanded detail accessibly.

An absent signal is explicit: for example, "No health report received yet" is
not shown as healthy, successful, or `none`.

### Status page

The Status page leads with a single current condition, the highest-priority
next action, and "Refreshed at" using existing observability freshness data.
Supporting metrics remain compact and have clear labels. Deep runtime details
remain available below the overview rather than competing with it.

The static eight-person employee dossier is removed. Status uses the canonical
AI employee team data, so it cannot diverge from the AI Employees module.

### AI Employees

Employees display as a compact responsive directory. Every person is visible
as a small card with name, role, department, and operating mode. An explicit
Expand button exposes their safe actions, approval rule, never-without-approval
items, productivity signals, character details, and technical metadata.

The canonical employee API is extended with the safe action and guardrail data
needed by the expanded panel. The existing compact organisation/profile pattern
is reused rather than maintaining a second fictional employee list.

### Control: customers and provisioning ledger

Customers become concise status cards. Each collapsed card displays customer
name, plain-language condition, concise explanation, next action, active
version with activation time, and last signal time. Expand reveals backup and
health detail/times, installed modules, rollout history/times, service keys,
and identifiers.

The provisioning ledger shows a readable target, current state, what happened,
and created/dispatched/updated/completed timestamps. Links to an external
deployment remain available, but are labelled by their purpose. Retry and
secret actions remain where allowed.

Tab metadata is explicit. For example, "3 recent provisioning runs" and "2
deployments" replace bare numerical badges. The Rollouts tab count is the
actual number of rollouts, not the number of deployments.

### Releases and rollouts

Release results are newest first, ordered by `created_at` descending with a
version tie-breaker. Any default release selection explicitly chooses the
newest eligible release rather than relying on list position.

Each release is a timeline card: candidate created, development started and
completed, development verified, offline signature attached, and customer
approval/paused state. Each reached stage has a timestamp. The next required
operator action is prominent; audit events are expandable.

Each rollout displays target version, plain-language state, started, last
reported, dispatched/completed times, and a failure reason when applicable.
Raw workflow/provider status and URLs are only in expanded detail.

### Navigation and account

Mission Control's left navigation includes an obvious Settings entry. The
account area is one clickable Settings/account entry and contains Logout. The
non-interactive role/location footer is removed, including the confusing "all
locations" text. Role labels are retained only where they clarify access.

## API and persistence changes

The operator API must expose data already persisted but currently dropped:

- Backup and health `created_at` plus useful detail.
- Rollout `created_at`, `dispatched_at`, `completed_at`, execution state,
  failure reason, and external run URL when present.
- Existing deployment version activation/heartbeat times, provisioning lifecycle
  times, release creation/promotion times, and observability freshness stay
  exposed and are rendered with `Timestamp`.

The deployment/provisioning persistence schema replaces bundle identity with
the selected optional module IDs. A development schema migration removes the
obsolete bundle field and uses the module selection as the recorded source of
truth. Release list queries sort newest-first at the API boundary.

## Error and empty states

- No eligible version: state which selected module lacks a publishable image
  and how to resolve it.
- No heartbeat or health report: say that no report has arrived and show the
  last known time if one exists.
- Failed rollout or provisioning run: show the user-facing failure summary,
  timestamp, and the permitted recovery action.
- No customers, releases, or rollouts: explain the next setup action rather
  than presenting an empty ledger.

## Implementation boundaries

The primary refactor splits the oversized operator surface into focused units:

```text
OperatorPanel
|- ControlOverview
|- CustomerList
|  `- CustomerCard + expandable detail
|- ProvisioningWizard
|  |- CoreModuleSummary
|  |- ModuleSelector
|  |- ReleaseRingHelp
|  `- ProvisioningReview
|- ProvisioningRunLedger
|- ReleaseTimeline
`- RolloutCard / RolloutTimeline

Shared
|- Timestamp
|- StatusSummary
|- ExpandableCard
`- Account menu / navigation
```

Expected backend ownership remains clear: module validation/composition belongs
to provisioning services; time/state data belongs to the control-plane API;
presentation and expansion state belong to the web UI.

## Verification

Tests will cover:

- Core-only and every supported optional-module combination.
- Rejection of unknown/duplicate module IDs and removal of old bundle inputs.
- Correct server resolution of spaces, apps, deployment modules, and module
  versions.
- Timestamp exposure for backup, health, rollout, provision, release, and
  fleet state.
- Newest-first release ordering and newest eligible default selection.
- Status wording and no-signal/failure empty states.
- Collapsed/expanded employee, customer, release, and rollout UI behavior.
- Focused Python tests plus web typecheck, lint, and production build.

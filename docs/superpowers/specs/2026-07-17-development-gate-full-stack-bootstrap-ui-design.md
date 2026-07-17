# Development Gate Full-Stack Bootstrap and Console Design

**Date:** 2026-07-17  
**Status:** Approved for implementation planning

## Purpose

Make the dedicated OneBrain development gate a real, customer-shaped `full_stack`
environment. It must contain the expected account, spaces, app installations,
AI Employees workspace, and audit state in its own database, while remaining
isolated from Mission Control. The console must also present the AI employee
organization compactly and remove misleading controls and counters.

## Current Problem

Mission Control provisions the development deployment with the `full_stack`
bundle and eight runtime modules, but the customer-side OneBrain database starts
with only the administrator user. The account, spaces, app installations, and
bootstrap audit record are created in Mission Control's provisioning metadata
store and are not recreated in the isolated customer database.

This produces four connected symptoms on the development gate:

- Apps reports zero accounts, spaces, apps, and audit events.
- AI Employees reports that it is not installed in an accessible workspace.
- The bootstrapped administrator is assigned to the legacy `nft_gym` tenant
  instead of the development account.
- Customer and Mission Control navigation are easy to confuse when an operator
  capability is accidentally present.

The AI Employees organization view also spends too much vertical space on a
large hero, five-metric strip, Chief of Staff office, leadership rail, and large
employee cards that repeat the same hierarchy.

## Product Boundaries

The development gate is a customer data plane with dummy data. It contains the
complete supported customer suite but is not Mission Control.

- **Development gate:** Status, Ask, Knowledge, KPIs, AI Employees, Apps,
  Privacy, and Settings.
- **Mission Control:** Status, Control, and Fleet.
- **Privacy:** remains on the development gate because it governs data in that
  customer-shaped environment.
- **Control and Fleet:** never appear on the development gate and their API
  routes remain absent.
- **Admin / DPO and All locations:** authorization metadata, not navigation
  destinations. They do not appear as inert sidebar items.

## Selected Approach

Use an idempotent, tenant-local bootstrap reconciler in the OneBrain API.
Provisioning supplies a small non-secret bootstrap descriptor to the customer
API. On startup, the API reconciles the bundle topology into its own platform
store.

This was selected over a cloud-init API-call sequence because startup
reconciliation uses the existing domain models, is retryable, and repairs an
already-running development gate on its next release. A seeded database snapshot
was rejected because it duplicates bundle logic and couples isolated deployments
to a mutable database artifact.

## Bootstrap Descriptor

The Hetzner renderer adds a URL-safe, base64-encoded JSON descriptor to the
`onebrain-api` environment for customer-role renders only. The descriptor
contains:

- schema version;
- account ID;
- account kind;
- customer display name;
- bundle ID.

The descriptor contains no password, service key, fleet key, provider
credential, or Mission Control URL. Structural identifiers are validated against
the existing provisioning vocabulary before rendering. The decoded document is
size-limited, rejects unknown fields, and must name an existing bundle.

Operator-role renders do not receive the descriptor. Local development remains
unchanged when it is absent.

## Tenant-Local Bootstrap Reconciler

The reconciler runs after schema validation and before the API begins serving
requests. It has one responsibility: make the local platform topology match the
explicit bootstrap descriptor.

For the `full_stack` development gate it ensures:

- one organization account for the provisioned account ID;
- Personal, Business, Customer service, Shared, and Family spaces;
- OneBrain Core, AI Assistant, AI Communication, KPI Dashboard, and AI
  Employees app installations;
- each installation's canonical enabled spaces and allowed purposes from
  `app/provisioning/bundles.py`;
- the account brand defaults;
- one deterministic `customer.bootstrap_reconciled` audit event.

The bundle definitions remain the single source of truth. The reconciler does
not copy customer rows from Mission Control and does not create control-plane
deployment, release, fleet, rollout, or provisioning records in the customer
database.

### Idempotency and partial repair

Every entity keeps the deterministic identifiers already used by provisioning.
The reconciler supports these states:

- empty database: create the full topology;
- complete topology: no writes;
- partial topology: create missing records;
- inactive or stale bootstrap-owned installation: restore the canonical bundle
  status, spaces, purposes, and display name;
- unrelated customer-created record: leave it unchanged.

PostgreSQL writes use conflict-safe upsert behavior so multiple API replicas
cannot create duplicates. The memory implementation follows the same observable
contract. The audit event has a deterministic ID and is emitted once.

### Administrator scope repair

The configured `ONEBRAIN_ADMIN_EMAIL` remains the only user eligible for
automatic bootstrap repair. On a customer deployment with a descriptor:

- a new administrator is created directly in the descriptor's account;
- an existing matching administrator assigned to legacy `nft_gym` is rebound
  to the descriptor's account, retains its password hash, and retains its
  password-change state;
- no other user's tenant, role, location, status, or password changes;
- existing sessions for a rebound user are revoked once so the next login creates
  a session with consistent account scope.

The location is an authorization property and is not used to create a sidebar
destination.

## Failure Behavior

An explicitly bootstrapped customer deployment fails startup if the descriptor
is malformed, oversized, references an unknown bundle, or cannot converge the
required account topology. It must not silently serve an empty workspace.

An absent descriptor is a supported no-op for Mission Control and local
development. A database connection or schema failure retains the existing
startup failure behavior. Errors identify the failed bootstrap stage and record
IDs but never log descriptor-adjacent secrets or environment dumps.

## Console Navigation and Identity

Navigation is derived from server-issued deployment capabilities:

- `operator_mode`: Status, Control, Fleet;
- customer mode: Status plus the customer navigation set;
- customer deployments cannot gain Control or Fleet from the user's admin role.

The redundant sidebar identity footer is removed. The command bar contains one
clear account/settings link using the administrator's display name or email.
Role and location remain available in Settings and authorization responses but
are not styled as nonfunctional navigation.

The Apps page must distinguish loading, loaded-empty, and failed states. Metric
values do not display as authoritative zeroes while the initial request is in
flight or has failed. After bootstrap, the development gate shows one account,
five spaces, five installed apps, and at least the bootstrap audit event.

## AI Employees Organization Layout

The selected visual direction is the compact organization map.

- Replace the oversized hero with a slim module header containing workspace,
  installation state, active employee count, open missions, and pending
  approvals.
- Remove the duplicate leadership-council rail.
- Show the Chief of Staff office as the hierarchy root.
- Show Operations & corporate, Product/technology/security, and Market/customer
  as three compact pod columns beneath it.
- Render each employee as a compact name-and-role row rather than a large card.
- Preserve the existing click-to-open profile sheet for biography, reporting
  line, model, personality, strengths, watch-outs, and working style.
- Preserve visible keyboard focus, meaningful button labels, Escape/close
  behavior, and reduced-motion behavior.
- At narrow widths, stack the hierarchy and pods in one column without hiding
  employees or requiring horizontal scrolling.

The organization map is the only substantial visual signature. The surrounding
module interface remains restrained so the hierarchy is easy to scan.

## AI Employees Activation

AI Employees remains an in-process OneBrain app installation rather than a
separate deployment module. Once the `ai_employees` installation exists in the
Business and Shared spaces, the existing workspace endpoint exposes those
spaces and the existing lazy default-team seeding produces the governed roster.

The development gate still reports all eight runtime modules from the
`full_stack` deployment manifest:

- `onebrain-api`, `onebrain-admin-ui`, `onebrain-workers`;
- `assistant-service`;
- `communication-api`, `communication-widget`, `communication-voice`, and
  `communication-workers`.

The five platform app installations and eight runtime modules are related but
different inventories; the UI and tests must not conflate their counts.

## Verification

### Backend and provisioning tests

- Render a customer box with a valid, non-secret bootstrap descriptor.
- Prove operator renders omit the descriptor.
- Reject malformed, oversized, unsafe, and unknown-bundle descriptors.
- Bootstrap an empty memory platform store and assert the full bundle topology.
- Run bootstrap twice and assert no duplicate records or audit events.
- Repair a partial topology and a stale bootstrap-owned installation.
- Rebind only the configured legacy administrator without changing its password.
- Revoke sessions only when an administrator scope rebind occurs.
- Exercise equivalent PostgreSQL conflict-safe behavior.
- Assert the `full_stack` runtime module set remains exactly eight modules.

### API and frontend tests

- List one account, five spaces, five apps, and bootstrap audit history on the
  development gate.
- List accessible AI Employees workspaces and load the default team.
- Prove customer sessions receive no Control/Fleet navigation and operator mode
  receives no customer navigation.
- Prove Privacy remains present on the customer surface.
- Prove Apps renders loading and error states instead of misleading zeroes.
- Exercise every employee profile control by keyboard.
- Verify the organization map at desktop, tablet, and mobile widths.
- Run Python tests, frontend tests, lint, type checking, and a production build.

## Acceptance Criteria

The change is complete when a newly provisioned or upgraded development gate:

1. reports the eight healthy `full_stack` runtime modules;
2. contains its own account, five canonical spaces, five canonical app
   installations, brand defaults, and bootstrap audit event;
3. opens AI Employees in an accessible Business or Shared workspace;
4. presents the compact organization map and detailed profile sheet;
5. shows Privacy but never Control or Fleet;
6. presents a single functional Settings identity link without inert
   Admin / DPO or All locations sidebar labels; and
7. can restart or run multiple API replicas without duplicating or resetting
   customer-created state.

## Rollout

Ship the reconciler and renderer changes together. The next development-gate
release supplies the descriptor and repairs the existing empty topology during
startup. Verify Apps, AI Employees, customer navigation, and Mission Control
isolation before marking that release dev-verified. Customer releases receive
the same bundle-scoped bootstrap behavior only when their renderer supplies a
descriptor; no existing deployment without one changes automatically.

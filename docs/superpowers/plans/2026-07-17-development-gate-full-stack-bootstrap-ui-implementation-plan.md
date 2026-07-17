# Development Gate Full-Stack Bootstrap and Console Implementation Plan

**Date:** 2026-07-17

**Design:** `docs/superpowers/specs/2026-07-17-development-gate-full-stack-bootstrap-ui-design.md`

## Objective

Make a customer-role `full_stack` Hetzner deployment self-contained and
testable: bootstrap its local platform topology and integration credentials,
scope its administrator correctly, expose AI Employees, keep Mission Control
surfaces absent, and replace the oversized employee directory with the approved
compact organization map.

## Design direction

- **Subject:** an operating-company organization map for a project
  administrator.
- **Single job:** let the administrator understand the team hierarchy and open
  one employee's details without scrolling through repeated summaries.
- **Palette:** ink `#111b24`, hierarchy navy `#1f3c52`, secondary blue
  `#527487`, action signal `#c2603d`, paper `#fbfcfc`, mist `#e9eef0`.
- **Type:** Aptos Display/Segoe UI Variable Display for the compact module title,
  the existing system sans stack for names and roles, and the existing utility
  treatment for status labels.
- **Layout:** a slim module summary above a Chief of Staff root and three pod
  columns; one column on narrow screens.
- **Signature:** the hierarchy spine connecting the Chief of Staff office to
  the three accountable pods.

This direction keeps OneBrain's existing AI module palette instead of adding a
generic dashboard theme. The hierarchy spine is the one expressive element;
the oversized hero, repeated leadership rail, and decorative metric blocks are
removed.

## Implementation order

### 1. Define and test the customer bootstrap descriptor

**Files**

- Add `app/provisioning/customer_bootstrap.py`.
- Extend `app/provisioning/hetzner/render.py`.
- Extend `app/provisioning/hetzner/provisioner.py`.
- Extend `tests/test_hetzner_render.py` and
  `tests/test_hetzner_provisioner.py`.

**Tests first**

- Encode and decode the versioned descriptor deterministically.
- Reject malformed base64, oversized payloads, unknown fields, unsafe account
  identifiers, invalid account kinds, and unknown bundles.
- Customer-role `onebrain-api` env receives the descriptor; operator-role env
  omits it.
- A full-stack provision uses the run's account, bundle, deployment display
  name, and account kind.

**Implement**

- Use a bounded URL-safe base64 JSON payload with no secrets.
- Validate it both before rendering and at application startup.
- Add `account_kind` to the provisioning run payload so development gates retain
  their `project` account shape.

### 2. Deliver distinct Assistant and Communication credentials

**Files**

- Extend `app/fleet/bootstrap_bundle.py`.
- Extend `app/provisioning/service.py` only where credential selection metadata
  is needed.
- Extend `app/routers/provisioning.py`.
- Extend `app/provisioning/hetzner/provisioner.py` and
  `app/provisioning/hetzner/render.py`.
- Extend bootstrap-bundle, provisioning, renderer, and provisioner tests.

**Tests first**

- Full-stack provisioning selects the Assistant key for Assistant only and the
  Communication key for Communication API/workers only.
- The rendered services no longer share the first arbitrary credential.
- The customer API receives both raw keys through secret references, never
  cloud-init plaintext.
- Missing required integration credentials fail a full-stack provision before
  a server is created.

**Implement**

- Add separate sealed bundle entries for Assistant and Communication.
- Keep module-facing environment names compatible by mapping the correct secret
  reference to each service's `ONEBRAIN_SERVICE_KEY`.
- Retain the existing generic entries only as a documented legacy fallback for
  bundles that do not run both integrations.

### 3. Add idempotent local topology and credential reconciliation

**Files**

- Extend `app/provisioning/customer_bootstrap.py`.
- Extend platform and service-key store protocols/implementations with narrow
  conflict-safe bootstrap upserts.
- Extend `app/main.py` and `app/deps.py` only as needed to invoke the reconciler.
- Add `tests/test_customer_bootstrap.py`.
- Extend PostgreSQL schema/store validation tests.

**Tests first**

- Empty local stores receive the canonical account, five spaces, five apps,
  brand defaults, two integration key records, and one audit event.
- A second run performs no observable duplicate writes.
- A partial run creates missing entities and repairs only bootstrap-owned stale
  app installations.
- Parsed key IDs and hashes match the raw per-app secrets; plaintext is never
  persisted.
- Assistant and Communication keys receive only their canonical app, spaces,
  purposes, and scopes.
- An invalid explicit descriptor or required-key mismatch fails startup.
- Mission Control and descriptor-free local development perform no bootstrap.

**Implement**

- Reuse `BUNDLES` as the topology and purpose source of truth.
- Use deterministic IDs and database conflict-safe upserts.
- Store only service-key hashes after parsing the injected raw keys.
- Emit one deterministic `customer.bootstrap_reconciled` audit event.

### 4. Correct the bootstrap administrator scope

**Files**

- Extend `app/users/base.py`, `app/users/memory.py`, and
  `app/users/postgres.py` with a narrow identity-scope update operation.
- Extend `app/users/seed.py`.
- Extend `app/main.py`.
- Extend auth and customer-bootstrap tests.

**Tests first**

- A new configured admin is created in the descriptor account.
- Only the configured admin may move from legacy `nft_gym` to the descriptor
  account.
- Password hash, password-change flag, email, display name, and status survive
  the repair.
- Existing sessions are revoked only when a scope repair occurs.
- No arbitrary cross-tenant user update is exposed through an API route.

**Implement**

- Seed the admin with the explicit account instead of the historical default.
- Rebind the exact configured legacy row and revoke its old sessions.

### 5. Make console navigation and Apps state unambiguous

**Files**

- Refactor `onebrain-web/src/components/console-shell.tsx`.
- Refactor `onebrain-web/src/components/spaces-panel.tsx`.
- Add pure navigation/state helpers and Node tests under `onebrain-web/tests/`.
- Adjust `onebrain-web/src/app/globals.css`.

**Tests first**

- Customer mode contains customer navigation including Privacy and Settings,
  without Control or Fleet.
- Operator mode contains Status, Control, and Fleet without customer modules.
- The sidebar does not render the inert role/location identity block.
- Apps initial loading and failed requests do not present authoritative zero
  metrics; a genuine loaded-empty state remains explicit.

**Implement**

- Keep navigation derived from server-issued deployment capabilities.
- Use one clear account link to Settings in the command bar.
- Track Apps initial/error/loaded state separately from its arrays.

### 6. Build the compact AI Employees organization map

**Files**

- Refactor `onebrain-web/src/components/ai-employees-panel.tsx`.
- Refactor `onebrain-web/src/components/ai-employee-organization.tsx`.
- Adjust the AI Employees section in `onebrain-web/src/app/globals.css`.
- Add focused pure tests where DOM infrastructure is unavailable.

**Tests first**

- The hierarchy contains the Chief of Staff root, Chief of Staff office, and
  three operating pods without duplicating the leadership council.
- Every employee remains a named button that opens the profile.
- Escape and the close button dismiss the profile; focus returns to the employee
  trigger.
- The compact header reports workspace, installation state, and employee count;
  operational counts remain visible in their tab badges.

**Implement**

- Remove the oversized hero, five-cell pulse strip, and leadership rail.
- Use module-level static pod metadata and memoized ID lookup.
- Render the approved hierarchy spine and compact rows.
- Stack pods at tablet/mobile widths, preserve focus visibility, and disable
  movement under reduced motion.

### 7. Verify the full development gate contract

**Checks**

```powershell
python -m pytest tests/test_customer_bootstrap.py tests/test_provisioning.py tests/test_bootstrap_bundle.py tests/test_hetzner_render.py tests/test_hetzner_provisioner.py -q
python -m pytest tests/test_ai_employee_api_scope.py tests/test_service_platform_scope.py tests/test_assistant_contracts.py tests/test_auth.py -q
npm --prefix onebrain-web test
npm --prefix onebrain-web run lint
npm --prefix onebrain-web run typecheck
npm --prefix onebrain-web run build
python -m pytest -q
git diff --check
```

Render the organization page at desktop, tablet, and mobile widths and inspect
the screenshots. Confirm that the current dirty hardening worktree was not
staged or overwritten.

## Done criteria

- The development gate's local database contains one account, five spaces, five
  app installations, brand defaults, local per-app service-key records, and a
  bootstrap audit event.
- Assistant and Communication authenticate with distinct least-privilege keys.
- AI Employees opens in Business or Shared and renders the compact organization
  map.
- Privacy remains visible; Control and Fleet remain Mission Control-only.
- Apps never presents loading or failure as unexplained zero state.
- Focused and repository-wide checks pass.
- Shipping is attempted only if the pre-existing unrelated hardening worktree
  has been resolved; otherwise the verified task changes remain unshipped and
  the blocker is reported.

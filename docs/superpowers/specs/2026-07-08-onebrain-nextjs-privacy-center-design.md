# OneBrain Next.js Privacy Center Design

Date: 2026-07-08

## Decision

Add a `/privacy` route to the Next.js console for admin/DPO privacy operations, while keeping all privacy enforcement, export assembly, erase execution, and audit writing in the Python/FastAPI backend.

This is Option A: a focused Privacy Center rather than a broad operator console.

## Goals

- Bring privacy export and erase workflows into the Next.js console.
- Reuse the existing Python privacy endpoints:
  - `GET /api/privacy/accounts/{account_id}/export`
  - `POST /api/privacy/accounts/{account_id}/erase`
- Reuse platform account and space endpoints for selection.
- Support account-wide and space-scoped operations.
- Require exact account-id confirmation before erase.
- Show clear result summaries after export or erase.
- Keep non-admin users blocked from the route.

## Non-Goals

- Do not rewrite privacy logic in TypeScript.
- Do not add a full operator/provisioning dashboard in this slice.
- Do not add account or space creation/editing in this slice.
- Do not change backend erase semantics.
- Do not remove the existing static operator UI yet.
- Do not run destructive smoke tests against existing local/demo accounts.

## Architecture

Add an admin route and client panel:

```text
onebrain-web
  src/
    app/
      privacy/page.tsx
    components/
      privacy-panel.tsx
      console-shell.tsx
    lib/
      onebrain-client.ts
      onebrain-types.ts
```

`/privacy/page.tsx` checks the current session using the existing server helper. If the user is not signed in, it shows the signed-out state. If the API is unavailable, it shows the API-unavailable state. If the user is not an admin, it renders a compact blocked state inside the console shell.

`PrivacyPanel` is a client component. It loads platform accounts, then spaces for the selected account. It calls privacy export and erase through the existing `/api/onebrain/...` proxy.

## Navigation

`ConsoleShell` should make `Privacy` an active route link instead of a disabled future item. `Chat` and `Documents` remain unchanged. `Spaces` and `Operator` can remain disabled future items.

## Data Types

Add typed privacy models:

- `PrivacyAuditEvent`
- `PrivacyExport`
- `PrivacyEraseResult`
- `PrivacyEraseInput`

The export model should include:

- `account_id`
- `space_id`
- `exported_at`
- `documents`
- `conversations`
- `intake_records`
- `audit_events`

The erase result should include:

- `account_id`
- `space_id`
- `documents_deleted`
- `chunks_deleted`
- `conversations_deleted`
- `intake_records_deleted`
- `audit_event_id`

## UI Behavior

The route renders:

- account selector
- optional space selector with `All account data`
- status badge
- `Export JSON` button
- confirmation input for erase
- optional reason input
- `Erase data` button
- result summary

Export behavior:

- The export button is enabled when an account is selected.
- Export calls the Python endpoint through the Next proxy.
- The browser downloads `onebrain-privacy-{account_id}.json`, with `-{space_id}` when space-scoped.
- The page also renders summary chips for document count, chunk count, conversation count, intake record count, and audit event count.

Erase behavior:

- The erase button is disabled until the confirmation input exactly matches the selected account id.
- Erase sends `confirm_account_id`, optional `space_id`, and optional `reason`.
- After erase, the confirmation and reason inputs are cleared.
- The page renders deleted counts and the audit event id.
- The page refreshes account/space data after erase.

## Error Handling

- Non-admin users see a blocked state and no controls.
- If account loading fails, show an inline error and keep controls disabled.
- If space loading fails, keep the account selected and show `All account data` only.
- If export fails, show the backend message and do not download a file.
- If erase fails, show the backend message and keep the confirmation input so the user can correct the issue.
- If no accounts exist, show an empty state instead of disabled mystery controls.

## Safety

Python remains the source of truth for admin checks, account/space validation, export assembly, erase execution, and audit writing.

The frontend adds friction for destructive action but does not replace backend confirmation. The backend `confirm_account_id` requirement remains mandatory.

Runtime smoke tests should not erase existing local/demo accounts. Destructive smoke can only run against a temporary account created for the test and cleaned immediately after, or be skipped with an explicit note.

## Testing

Run:

- Python test suite
- Next.js typecheck
- Next.js lint
- npm audit at the existing threshold
- Next.js production build

Runtime smoke should verify:

- `/privacy` loads for admin.
- `/privacy` blocks non-admin.
- platform account loading works through the proxy.
- export returns JSON for a selected account when an account is available.
- erase controls remain disabled until exact confirmation in UI-level behavior where practical.
- destructive erase is not run against existing local data.

## Rollout

This is additive. The existing FastAPI static operator/privacy UI remains available. After this route is verified, the next natural slice is either a richer `/spaces` admin page or the broader `/operator` route.

## Spec Self-Review

- No placeholders remain.
- The route is scoped to privacy export and erase only.
- Backend ownership of privacy behavior is explicit.
- Destructive-test boundaries are explicit.
- Non-admin behavior is explicit.

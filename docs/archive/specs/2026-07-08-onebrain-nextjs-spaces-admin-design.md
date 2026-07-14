# OneBrain Next.js Spaces Admin Design

## Summary

Add a `/spaces` route to the Next.js console for admin account, space, app-installation, access-check, and audit workflows. This is Option B: use the existing Python/FastAPI platform APIs first, and add only narrow backend capabilities if the implementation proves a current workflow cannot be completed through those APIs.

The Python backend remains authoritative for authentication, admin checks, platform validation, audit recording, account ownership, space ownership, app permissions, and access decisions. Next.js provides the product UI and typed client boundary only.

## Goals

- Bring platform account and space management into the Next.js console.
- Reuse existing FastAPI platform endpoints:
  - `GET /api/platform/accounts`
  - `POST /api/platform/accounts`
  - `GET /api/platform/accounts/{account_id}/spaces`
  - `POST /api/platform/accounts/{account_id}/spaces`
  - `GET /api/platform/accounts/{account_id}/apps`
  - `POST /api/platform/accounts/{account_id}/apps`
  - `POST /api/platform/access/check`
  - `GET /api/platform/accounts/{account_id}/audit`
- Let admins create accounts and spaces, install apps, run access checks, and inspect account audit history.
- Keep the existing workspace selector behavior intact for chat/documents.
- Keep the existing FastAPI static operator UI available.

## Non-Goals

- Do not rewrite platform governance logic in TypeScript.
- Do not add account, space, or app edit/archive controls in this slice.
- Do not migrate the broader operator deployment dashboard in this slice.
- Do not change retrieval, document ingestion, privacy erasure, or service-key behavior.
- Do not make non-admin users able to call platform endpoints from the UI.

## Existing Backend Contract

The existing `app/routers/platform.py` already exposes the core actions needed for the first `/spaces` page:

- list/create accounts
- list/create spaces under an account
- list/install app installations under an account
- check app access for one account/app/space/purpose combination
- list audit events for an account

The store contract in `app/platform/base.py` supports creation and lookup, but does not expose update/archive methods. Therefore, this first slice should avoid edit/archive UI rather than inventing incomplete frontend-only states.

## Needed Additions

No new backend endpoint is planned for the first implementation unless code-level integration reveals a missing response field or validation gap.

The expected additions are in the Next.js app:

```text
onebrain-web/src/app/
  spaces/
    page.tsx

onebrain-web/src/components/
  spaces-panel.tsx

onebrain-web/src/lib/
  onebrain-client.ts
  onebrain-types.ts
```

Update shared files:

- `onebrain-web/src/components/console-shell.tsx` to promote `Spaces` from future nav to active primary nav.
- `onebrain-web/src/app/globals.css` for spaces-specific layout and forms.
- `onebrain-web/README.md` to document the route and ownership boundary.

## Route Behavior

`/spaces/page.tsx` uses the existing server session helper.

- If the FastAPI API is unavailable, render `ApiUnavailableState`.
- If the user is not signed in, render `SignedOutState`.
- If the user is not an admin, render a compact blocked state inside `ConsoleShell active="spaces"`.
- If the user is an admin, render `ConsoleShell active="spaces"` with `SpacesPanel`.

The route does not perform platform mutation on the server. Mutating actions stay in the client panel and go through the existing `/api/onebrain/...` proxy so FastAPI sees the signed session cookie and enforces admin authorization.

## UI Design

The `/spaces` page should feel like an operational admin surface, not a marketing page. It should use the existing console shell and quiet card language from documents/privacy.

Primary regions:

1. Header
   - title: `Spaces`
   - subtitle/eyebrow: `Platform admin`
   - refresh button

2. Account rail
   - account list with name, id, kind, status
   - create account form
   - selected account highlight

3. Space management
   - selected account summary
   - spaces list/table with name, id, kind, status
   - create space form

4. App installations
   - installed apps list
   - install app form with:
     - app id
     - display name
     - enabled spaces multi-select
     - allowed purposes multi-select

5. Access check
   - choose app id, space, and purpose
   - submit to FastAPI access-check endpoint
   - show allowed/denied result and backend reason

6. Audit feed
   - latest account audit events
   - action, target, decision/reason, purpose/app/space metadata where available

The layout should be dense but readable:

- desktop: account rail on the left, management/detail panels on the right
- mobile: stack account list, then management panels
- no nested cards
- all long ids must wrap or truncate cleanly
- destructive-looking styling is not needed because this slice has no destructive actions

## Data Model Additions

Add typed models matching backend responses:

- `PlatformAppInstallation`
- `PlatformAuditEvent`
- `PlatformAccessCheckInput`
- `PlatformAccessCheckResult`
- `CreatePlatformAccountInput`
- `CreatePlatformSpaceInput`
- `InstallPlatformAppInput`

Existing models can be reused:

- `PlatformAccount`
- `PlatformSpace`

The client helpers should be:

- `createPlatformAccount(input)`
- `createPlatformSpace(accountId, input)`
- `listPlatformApps(accountId)`
- `installPlatformApp(accountId, input)`
- `checkPlatformAccess(input)`
- `listPlatformAudit(accountId)`

Existing helpers stay:

- `listPlatformAccounts()`
- `listPlatformSpaces(accountId)`

## Data Flow

Initial load:

1. `SpacesPanel` loads accounts.
2. The first account becomes selected unless the previous selected account still exists after refresh.
3. When selected account changes, the panel loads spaces, apps, and audit in parallel.
4. Empty states render when an account has no spaces, apps, or audit events.

Create account:

1. Admin submits kind/name/optional id.
2. FastAPI validates and creates the account.
3. The panel refreshes accounts and selects the new account.

Create space:

1. Admin submits kind/name/optional id for the selected account.
2. FastAPI validates account ownership and creates the space.
3. The panel refreshes spaces, apps, and audit.

Install app:

1. Admin selects app id, enabled spaces, allowed purposes, optional display name, optional id.
2. FastAPI validates app id, purposes, account, and space membership.
3. The panel refreshes apps and audit.

Access check:

1. Admin selects app, space, and purpose.
2. FastAPI returns allowed/denied plus reason.
3. The panel displays the decision and refreshes audit because the backend records the check.

Audit:

1. Audit loads per selected account.
2. Refresh actions update the feed.

## Error Handling

- Platform API unavailable: page-level API-unavailable state only for session fetch failures.
- Account list failure inside the client panel: inline error and empty operational state.
- Selected account deleted externally: refresh falls back to the first available account.
- Space/app/audit load failure: inline error, preserve already loaded data where possible.
- Create/install validation errors: show FastAPI `detail` text inline.
- Access denied for non-admin API calls: blocked page from the server route, and FastAPI `403` if a stale client action still fires.

## Security And Privacy

- Next.js does not decide whether a user may manage platform data.
- All admin checks stay in FastAPI.
- The page must never expose platform APIs to non-admins through server-rendered data.
- Account and space ids are operational identifiers; render them carefully but do not hide them from admins.
- App-install permissions are purpose-based and must be sent to FastAPI as explicit lists.
- Access-check decisions shown in the UI must be backend decisions, not client-side predictions.

## Testing Plan

Automated checks:

- Next.js typecheck
- Next.js lint
- Python pytest suite
- npm audit
- Next.js production build

Runtime smoke:

- `/spaces` loads for admin.
- `/spaces` blocks non-admin.
- Admin can list accounts through the Next proxy.
- Admin can list spaces/apps/audit for an account through the Next proxy.
- Admin can run one access check through the Next proxy.
- Non-admin receives `403` from a platform API call through the Next proxy.

Mutation smoke:

- Prefer a temporary account id and temporary space id if running create-account/create-space/install-app smoke.
- If no temporary account is created, do not mutate existing production-like account data.
- Do not add archive/delete smoke because this slice has no destructive action.

## Acceptance Criteria

- `/spaces` is visible in the primary console nav.
- `Spaces` is no longer shown as a disabled future nav item.
- Admins can manage the existing platform workflows from Next.js:
  - create account
  - create space
  - install app
  - run access check
  - inspect audit
- Non-admins see a clear blocked state.
- Backend remains Python/FastAPI authority for all platform behavior.
- Existing `/chat`, `/documents`, `/privacy`, and workspace selector behavior continue to pass checks.

## Future Work

After this page is shipped and verified, consider a backend-governed platform lifecycle slice:

- update account name/status
- update space name/status
- archive/reactivate account
- archive/reactivate space
- update app installation enabled spaces/purposes

Those actions require explicit store methods, FastAPI endpoints, audit events, tests, and careful UI states. They should not be squeezed into this first `/spaces` migration.


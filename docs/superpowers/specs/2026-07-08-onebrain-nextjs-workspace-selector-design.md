# OneBrain Next.js Workspace Selector Design

Date: 2026-07-08

## Decision

Add an admin-only workspace selector to the Next.js console, while keeping platform account, space, authorization, and scope enforcement in the Python/FastAPI backend.

The selector lets admins switch between the normal role-based view and a specific `{ account_id, space_id }` scope. Chat, conversations, document listing, upload, and pending review should all use the selected scope.

## Goals

- Bring the existing static UI workspace selector into the Next.js console.
- Keep the selector global across `/chat` and `/documents`.
- Allow admins to choose:
  - all visible data
  - one account space
- Pass selected scope into existing FastAPI-backed calls.
- Reset route-local state when scope changes so data from one workspace does not linger in another.
- Keep non-admin users on the current unscoped role-based experience.

## Non-Goals

- Do not rewrite platform account or space APIs.
- Do not add account/space creation or editing in this slice.
- Do not add a full `/spaces` admin management page in this slice.
- Do not move access-control checks into Next.js.
- Do not persist scope server-side in this slice.
- Do not remove the existing static UI yet.

## Architecture

Add a client-side workspace context around the routed console:

```text
onebrain-web
  src/
    components/
      console-shell.tsx
      workspace-provider.tsx
      workspace-selector.tsx
      chat-panel.tsx
      documents-panel.tsx
    lib/
      onebrain-client.ts
      onebrain-types.ts
```

`WorkspaceProvider` owns the current `ChatScope` and the loaded account/space options. `ConsoleShell` renders `WorkspaceSelector` for admins. `ChatPanel` and `DocumentsPanel` read the selected scope from context and pass it to their client calls.

The route pages can keep their current server prefetches as unscoped defaults. After hydration, the client components refresh using the selected scope. This keeps the implementation small and avoids duplicating query-string state in server components during this slice.

## Data Model

Add typed platform models:

- `PlatformAccount`
  - `id`
  - `kind`
  - `name`
  - `owner_user_id`
  - `status`
- `PlatformSpace`
  - `id`
  - `account_id`
  - `kind`
  - `name`
  - `status`

Reuse the existing `ChatScope` type:

```ts
type ChatScope = {
  account_id?: string;
  space_id?: string;
};
```

Scope is considered active only when both `account_id` and `space_id` are present. An empty scope means all visible data for the current role.

## Components

### WorkspaceProvider

Responsibilities:

- Load accounts from `GET /api/platform/accounts` for admins.
- Load spaces from `GET /api/platform/accounts/{account_id}/spaces`.
- Default to the signed-in tenant account when available.
- Default to the first available space for that account.
- Store the active scope in component state.
- Expose:
  - current scope
  - account and space options
  - loading/error state
  - selected account and space
  - setters for account and space selection

If loading fails, the provider should expose an unavailable state and an empty scope.

### WorkspaceSelector

Responsibilities:

- Render only for `session.role_id === "admin"`.
- Show account select when accounts are available.
- Show space select with an `All visible data` option.
- Show a compact badge for the selected space kind.
- Disable controls while account/space options are loading.
- Hide itself if platform APIs are unavailable.

The selector belongs in the console sidebar near the identity/navigation area, because it affects the whole console.

### ChatPanel

Changes:

- Read `scope` from `WorkspaceProvider`.
- Pass scope to:
  - `listConversations`
  - `getConversation`
  - `deleteConversation`
  - `askStream`
- When scope changes:
  - clear the selected conversation
  - clear messages
  - refresh conversation list

### DocumentsPanel

Changes:

- Read `scope` from `WorkspaceProvider`.
- Pass scope to:
  - `listDocuments`
  - `listPendingDocuments`
  - `uploadDocument`
  - `approveDocument`
- When scope changes:
  - refresh visible documents and pending review queue
  - keep the upload form labels and selected file unchanged unless the upload is in progress

## API and Data Flow

Browser calls continue to go through `/api/onebrain/...`.

Add typed client functions:

- `listPlatformAccounts()`
- `listPlatformSpaces(accountId)`

Existing typed functions should accept and use the selected scope:

- `listConversations(scope)`
- `getConversation(id, scope)`
- `deleteConversation(id, scope)`
- `askStream(payload)`
- `listDocuments(scope)`
- `listPendingDocuments(scope)`
- `uploadDocument(input, scope)`
- `approveDocument(id, scope)`

The Python backend remains responsible for verifying that the caller may use a scoped account/space. If a non-admin user somehow sends scoped parameters, backend policy still decides the result.

## Error Handling

- If platform account loading returns `403`, hide the selector and keep unscoped behavior.
- If platform account loading fails for another reason, hide the selector and keep unscoped behavior.
- If spaces fail to load for an account, keep the prior scope until the user chooses another valid option or selects all visible data.
- If a scoped chat or document request fails after scope change, show the existing inline error for that route.
- Scope changes should not silently reuse old messages, old conversation IDs, or old document counts.

## UI Direction

The selector should stay compact and operational:

- Use native selects for account and space.
- Use a small status badge for the selected mode.
- Keep labels plain: `Account`, `Space`, and `All visible data`.
- Do not introduce a large workspace page, modal, or wizard for this slice.
- Preserve the current console palette and restrained styling.

## Testing

Run:

- Python test suite
- Next.js typecheck
- Next.js lint
- npm audit at the existing threshold
- Next.js production build

Runtime smoke should verify:

- admin can load `/chat` and `/documents`
- workspace selector data loads through the proxy
- switching scope changes chat conversation list requests
- scoped chat streaming still works
- scoped document listing works
- scoped upload still sends account and space fields
- non-admin session keeps unscoped behavior

## Rollout

This is additive. Existing unscoped behavior remains the fallback. After this slice, the next natural migration is either privacy/export controls or a richer spaces/admin page.

## Spec Self-Review

- No placeholders remain.
- The backend ownership boundary is explicit.
- The selector is global but small enough for one implementation slice.
- Scope reset behavior is explicit for chat and documents.
- Non-admin behavior remains unchanged.
